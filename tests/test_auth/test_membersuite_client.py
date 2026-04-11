"""Tests for MemberSuiteClient.authenticate_and_verify membership bypass."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.exceptions import MembershipRequiredError
from app.auth.membersuite_client import MemberSuiteClient, MemberSuiteUser


def _make_user(email: str, receives_member_benefits: bool | None) -> MemberSuiteUser:
    return MemberSuiteUser(
        tenant_id=1,
        association_id="assoc-1",
        user_id="user-1",
        email=email,
        first_name="Test",
        last_name="User",
        owner_id="owner-1",
        membership_id=None,
        receives_member_benefits=receives_member_benefits,
        username="testuser",
    )


@pytest.fixture
def client(monkeypatch):
    """MemberSuiteClient with mocked HTTP and token exchange."""
    import httpx
    from app.auth.config import AuthSettings

    settings = AuthSettings(
        tenant_id="test-tenant",
        association_id="test-assoc",
        redis_url="redis://localhost",
        session_secret="x" * 32,
        frontend_url="http://localhost:5173",
    )
    http_client = MagicMock(spec=httpx.AsyncClient)
    return MemberSuiteClient(settings=settings, http_client=http_client)


class TestAuthenticateAndVerifyMembershipBypass:
    @pytest.mark.asyncio
    async def test_active_member_allowed(self, client):
        """Active members pass when require_membership=True."""
        user = _make_user("journalist@example.com", receives_member_benefits=True)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        auth_token, returned_user = await client.authenticate_and_verify(
            "guid-123", require_membership=True
        )
        assert auth_token == "token-abc"
        assert returned_user is user

    @pytest.mark.asyncio
    async def test_inactive_member_denied_when_required(self, client):
        """Non-members are denied when require_membership=True."""
        user = _make_user("journalist@example.com", receives_member_benefits=False)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        with pytest.raises(MembershipRequiredError):
            await client.authenticate_and_verify("guid-123", require_membership=True)

    @pytest.mark.asyncio
    async def test_inactive_member_allowed_when_not_required(self, client):
        """Non-members are allowed when require_membership=False."""
        user = _make_user("journalist@example.com", receives_member_benefits=False)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        auth_token, returned_user = await client.authenticate_and_verify(
            "guid-123", require_membership=False
        )
        assert auth_token == "token-abc"

    @pytest.mark.asyncio
    async def test_ire_staff_bypasses_membership_check(self, client):
        """IRE staff (@ire.org) bypass the membership check even when require_membership=True."""
        user = _make_user("staff@ire.org", receives_member_benefits=False)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        auth_token, returned_user = await client.authenticate_and_verify(
            "guid-123", require_membership=True
        )
        assert auth_token == "token-abc"
        assert returned_user is user

    @pytest.mark.asyncio
    async def test_ire_staff_no_membership_case_insensitive(self, client):
        """IRE staff email check is case-insensitive."""
        user = _make_user("STAFF@IRE.ORG", receives_member_benefits=None)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        auth_token, returned_user = await client.authenticate_and_verify(
            "guid-123", require_membership=True
        )
        assert auth_token == "token-abc"

    @pytest.mark.asyncio
    async def test_non_ire_domain_not_bypassed(self, client):
        """Emails at similar-but-different domains are still subject to membership check."""
        user = _make_user("user@notire.org", receives_member_benefits=False)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        with pytest.raises(MembershipRequiredError):
            await client.authenticate_and_verify("guid-123", require_membership=True)

    @pytest.mark.asyncio
    async def test_ire_subdomain_not_bypassed(self, client):
        """Subdomains of ire.org are not treated as IRE staff."""
        user = _make_user("user@staff.ire.org", receives_member_benefits=False)
        client.exchange_token_guid = AsyncMock(return_value="token-abc")
        client.get_user_info = AsyncMock(return_value=user)

        with pytest.raises(MembershipRequiredError):
            await client.authenticate_and_verify("guid-123", require_membership=True)
