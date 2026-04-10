"""Tests for the require_member dependency."""

import time

import pytest

from app.auth.dependencies import require_member
from app.auth.exceptions import MembershipRequiredError
from app.auth.session import Session


def _make_session(email: str, is_active_member: bool) -> Session:
    return Session(
        session_id="test-session-id",
        user_id="test-user-123",
        email=email,
        first_name="Test",
        last_name="User",
        full_name="Test User",
        is_active_member=is_active_member,
        membership_id=None,
        created_at=time.time(),
        expires_at=9999999999,
    )


class TestRequireMember:
    @pytest.mark.asyncio
    async def test_active_member_allowed(self):
        """Regular active members are allowed access."""
        session = _make_session("journalist@example.com", is_active_member=True)
        result = await require_member(session)
        assert result is session

    @pytest.mark.asyncio
    async def test_inactive_member_denied(self):
        """Non-members without an ire.org email are denied."""
        session = _make_session("journalist@example.com", is_active_member=False)
        with pytest.raises(MembershipRequiredError):
            await require_member(session)

    @pytest.mark.asyncio
    async def test_ire_staff_allowed_without_membership(self):
        """IRE staff (ire.org email) bypass the membership check."""
        session = _make_session("staff@ire.org", is_active_member=False)
        result = await require_member(session)
        assert result is session

    @pytest.mark.asyncio
    async def test_ire_staff_allowed_with_membership(self):
        """IRE staff with active membership are also allowed."""
        session = _make_session("staff@ire.org", is_active_member=True)
        result = await require_member(session)
        assert result is session

    @pytest.mark.asyncio
    async def test_ire_staff_email_case_insensitive(self):
        """Email domain check is case-insensitive."""
        session = _make_session("STAFF@IRE.ORG", is_active_member=False)
        result = await require_member(session)
        assert result is session

    @pytest.mark.asyncio
    async def test_non_ire_domain_denied(self):
        """Emails that merely contain ire.org but are not @ire.org are denied."""
        session = _make_session("attacker@notire.org", is_active_member=False)
        with pytest.raises(MembershipRequiredError):
            await require_member(session)

    @pytest.mark.asyncio
    async def test_ire_org_subdomain_denied(self):
        """Subdomains of ire.org (e.g. staff.ire.org) are denied — only @ire.org is allowed."""
        session = _make_session("user@staff.ire.org", is_active_member=False)
        with pytest.raises(MembershipRequiredError):
            await require_member(session)
