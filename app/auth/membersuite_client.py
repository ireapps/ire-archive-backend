"""MemberSuite REST API client for Reverse SSO.

Handles:
1. Initiating login (POST /platform/v2/signUpSSO)
2. Exchanging tokenGUID for AuthToken (GET /platform/v2/regularSSO)
3. Fetching user info (GET /platform/v2/whoami)

Reference: MemberSuite REST API Documentation (Revised March 2024)
         "Option 2: Reverse SSO (Common)" - pages 33-38

Key timings from official docs:
- tokenGUID is valid for 5 minutes after MemberSuite callback
- AuthToken is valid for 1 hour after exchange

API Base URL: https://rest.membersuite.com
Swagger Docs: https://rest.membersuite.com/platform/swagger/ui/index#/
"""

from dataclasses import dataclass

import httpx
import structlog

from app.auth.config import AuthSettings
from app.auth.exceptions import (
    AuthenticationError,
    MembershipRequiredError,
    MemberSuiteError,
    TokenExchangeError,
)

logger = structlog.get_logger()


@dataclass
class MemberSuiteUser:
    """User information from MemberSuite /whoami endpoint.

    Membership logic (from official docs page 9, 31, 37):
    - receivesMemberBenefits = true: Active member
    - receivesMemberBenefits = null: Never had a membership (non-member)
    - receivesMemberBenefits = false: Had membership that expired/dropped (non-member)

    For our purposes, only `true` grants access.
    """

    tenant_id: int
    association_id: str
    user_id: str
    email: str
    first_name: str
    last_name: str
    owner_id: str
    membership_id: str | None
    receives_member_benefits: bool | None  # True/False/None - see docstring
    username: str

    @property
    def is_active_member(self) -> bool:
        """True only if receivesMemberBenefits is explicitly True.

        None = never had membership, False = expired/dropped membership.
        Both are treated as non-member for access control.
        """
        return self.receives_member_benefits is True

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.username

    def to_session_dict(self) -> dict:
        """Convert to dict for session storage."""
        return {
            "user_id": self.user_id,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "is_active_member": self.is_active_member,
            "membership_id": self.membership_id,
        }


class MemberSuiteClient:
    """Async client for MemberSuite REST API."""

    def __init__(self, settings: AuthSettings, http_client: httpx.AsyncClient):
        self.settings = settings
        self.http = http_client
        self.base_url = settings.api_base_url

    async def get_login_redirect_url(self) -> str:
        """Get MemberSuite login page URL for Reverse SSO.

        Official API: POST /platform/v2/signUpSSO
        Content-Type: application/x-www-form-urlencoded

        Form params:
        - nextUrl: Where to redirect after login (our callback URL)
        - IsSignUp: "false" for login, "true" for registration
        - AssociationId: GUID identifying the association

        Per official docs: "The signUpSSO call only needs to be run once per
        nextURL passed to obtain that specific Location URL for SSO."

        Returns:
            URL to redirect user to for MemberSuite login
            Example: https://[acronym].users.membersuite.com/auth/portal-login?...

        Raises:
            MemberSuiteError: If API call fails or no Location header
        """
        endpoint = f"{self.base_url}/platform/v2/signUpSSO"

        logger.info(
            "membersuite_sso_initiate",
            callback_url=self.settings.callback_url,
        )

        try:
            response = await self.http.post(
                endpoint,
                data={
                    "nextUrl": self.settings.callback_url,
                    "IsSignUp": "false",  # Login, not registration
                    "AssociationId": self.settings.association_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,  # CRITICAL: We need the Location header
            )

            # MemberSuite returns 302/303 with Location header
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if location:
                    logger.info(
                        "membersuite_sso_url_obtained",
                        url_prefix=location[:50] if location else "",
                    )
                    return location

            logger.error(
                "membersuite_sso_no_redirect",
                status_code=response.status_code,
                headers=dict(response.headers),
                body_preview=response.text[:200] if response.text else "",
            )
            raise MemberSuiteError(f"Expected redirect from MemberSuite, got {response.status_code}")

        except httpx.RequestError as e:
            logger.error("membersuite_sso_request_failed", error=str(e))
            raise MemberSuiteError(f"Failed to connect to MemberSuite: {e}")

    async def exchange_token_guid(self, token_guid: str) -> str:
        """Exchange callback tokenGUID for AuthToken.

        IMPORTANT: tokenGUID expires in 5 minutes. Call immediately.

        Official API: GET /platform/v2/regularSSO
        Params: partitionKey (tenant ID), tokenGUID

        Args:
            token_guid: The tokenGUID from callback query string

        Returns:
            AuthToken string (valid for 1 hour)

        Raises:
            TokenExchangeError: If exchange fails
        """
        endpoint = f"{self.base_url}/platform/v2/regularSSO"

        logger.info(
            "membersuite_token_exchange",
            token_guid_prefix=token_guid[:8] if token_guid else "missing",
        )

        try:
            response = await self.http.get(
                endpoint,
                params={
                    "partitionKey": self.settings.tenant_id,
                    "tokenGUID": token_guid,
                },
            )

            if response.status_code != 200:
                logger.error(
                    "membersuite_token_exchange_failed",
                    status_code=response.status_code,
                    response_text=response.text[:200] if response.text else "",
                )
                raise TokenExchangeError(response.status_code)

            # Per official docs: Response body is a raw token string
            # Example: "MaiapmkmRRw/BKE4HsvDpT1bHRjfuJ0qW+a0DXITe/kr2SDHcNamIaaB..."
            # May be wrapped in quotes or JSON object depending on version
            token = response.text.strip()

            # Handle potential JSON wrapping
            if token.startswith('"') and token.endswith('"'):
                token = token[1:-1]
            elif token.startswith("{"):
                try:
                    data = response.json()
                    # Try common field names if it's JSON
                    token = data if isinstance(data, str) else str(data)
                except Exception:
                    pass  # Keep the raw text

            if not token:
                raise TokenExchangeError(response.status_code)

            logger.info("membersuite_token_exchange_success")
            return token

        except httpx.RequestError as e:
            logger.error("membersuite_token_exchange_error", error=str(e))
            raise MemberSuiteError(f"Token exchange request failed: {e}")

    async def get_user_info(self, auth_token: str) -> MemberSuiteUser:
        """Fetch user information using AuthToken.

        Official API: GET /platform/v2/whoami
        Authorization header: "AuthToken {token}" (note: NOT "Bearer")

        Response fields (from official docs):
        - tenantId: integer
        - associationId: string (guid)
        - userId: string (guid)
        - email: string
        - firstName: string
        - lastName: string
        - ownerId: string (guid)
        - membershipId: string (guid) or null
        - receivesMemberBenefits: boolean or null
        - paymentProcessor: string
        - businessUnit: string (guid)
        - merchantAccount: string (guid)
        - baseCurrency: string
        - username: string

        Args:
            auth_token: Valid AuthToken from exchange

        Returns:
            MemberSuiteUser with profile and membership info

        Raises:
            AuthenticationError: If token is invalid/expired
        """
        endpoint = f"{self.base_url}/platform/v2/whoami"

        try:
            response = await self.http.get(
                endpoint,
                headers={
                    "Accept": "application/json",
                    # IMPORTANT: Use "AuthToken" not "Bearer" for Reverse SSO tokens
                    "Authorization": f"AuthToken {auth_token}",
                },
            )

            if response.status_code != 200:
                logger.warning(
                    "membersuite_whoami_failed",
                    status_code=response.status_code,
                )
                raise AuthenticationError("Invalid or expired authentication token")

            data = response.json()

            user = MemberSuiteUser(
                tenant_id=data.get("tenantId", 0),
                association_id=data.get("associationId", ""),
                user_id=data.get("userId", ""),
                email=data.get("email", ""),
                first_name=data.get("firstName", ""),
                last_name=data.get("lastName", ""),
                owner_id=data.get("ownerId", ""),
                membership_id=data.get("membershipId"),
                receives_member_benefits=data.get("receivesMemberBenefits"),
                username=data.get("username", ""),
            )

            logger.info(
                "membersuite_user_fetched",
                user_id=user.user_id,
                is_member=user.is_active_member,
            )

            return user

        except httpx.RequestError as e:
            logger.error("membersuite_whoami_error", error=str(e))
            raise MemberSuiteError(f"Failed to fetch user info: {e}")

    async def authenticate_and_verify(
        self, token_guid: str, require_membership: bool = True
    ) -> tuple[str, MemberSuiteUser]:
        """Complete authentication flow: exchange token and verify membership.

        Args:
            token_guid: tokenGUID from callback
            require_membership: If True, raises error for non-members

        Returns:
            Tuple of (auth_token, user)

        Raises:
            TokenExchangeError: If token exchange fails
            AuthenticationError: If token invalid
            MembershipRequiredError: If membership required but not active
        """
        # Step 1: Exchange tokenGUID for AuthToken
        auth_token = await self.exchange_token_guid(token_guid)

        # Step 2: Fetch user info
        user = await self.get_user_info(auth_token)

        # Step 3: Verify membership if required
        # IRE staff (email addresses ending with @ire.org) bypass the membership check.
        is_ire_staff = user.email.lower().endswith("@ire.org")
        if require_membership and not is_ire_staff and not user.is_active_member:
            logger.warning(
                "membersuite_membership_denied",
                user_id=user.user_id,
                receives_benefits=user.receives_member_benefits,
            )
            raise MembershipRequiredError()

        return auth_token, user
