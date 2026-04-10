"""FastAPI dependencies for authentication.

Usage:
    @app.get("/protected")
    async def protected_route(session: Session = Depends(require_session)):
        return {"user": session.email}
"""

from typing import Optional

from fastapi import Cookie, Depends, Request

from app.auth.config import get_auth_settings
from app.auth.exceptions import SessionExpiredError
from app.auth.session import Session, SessionManager


async def get_session_cookie(
    request: Request,
    ire_session: str | None = Cookie(None, alias="ire_session"),
) -> str | None:
    """Extract session cookie value."""
    # Cookie name from settings
    settings = get_auth_settings()
    cookie_name = settings.session_cookie_name

    # Try explicit parameter first, then request.cookies
    return ire_session or request.cookies.get(cookie_name)


async def get_session_manager_dep(request: Request) -> SessionManager:
    """Get session manager from request state."""
    from app.dependencies import get_session_manager

    return get_session_manager(request)


async def get_optional_session(
    cookie: str | None = Depends(get_session_cookie),
    manager: SessionManager = Depends(get_session_manager_dep),
) -> Session | None:
    """Get current session if exists, otherwise None.

    Use for endpoints that work with or without authentication.
    """
    if not cookie:
        return None
    return await manager.get_session(cookie)


async def require_session(
    cookie: str | None = Depends(get_session_cookie),
    manager: SessionManager = Depends(get_session_manager_dep),
) -> Session:
    """Require valid session, raise 401 if not authenticated.

    Use for protected endpoints.
    """
    return await manager.get_session_or_raise(cookie)


async def require_member(
    session: Session = Depends(require_session),
) -> Session:
    """Require session with active membership.

    IRE staff (ire.org email addresses) are granted access regardless of
    membership status.

    Use for member-only endpoints.
    """
    is_ire_staff = session.email.lower().endswith("@ire.org")
    if not is_ire_staff and not session.is_active_member:
        from app.auth.exceptions import MembershipRequiredError

        raise MembershipRequiredError()
    return session
