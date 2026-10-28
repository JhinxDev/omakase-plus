"""FastAPI dependencies for Omakase Plus authentication.

Provides ``get_current_user`` (returns ``User | None``) and
``require_user`` (redirects browser page visits or raises 401 for API clients
when the session is missing or invalid).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from omakase.plus.auth import authenticate_session
from omakase.plus.deps import get_db
from omakase.plus.models import User
from omakase.plus.web_security import enforce_browser_csrf


async def get_current_user(
    request: Request,
    db=Depends(get_db),
) -> User | None:
    """Read ``omakase_session`` cookie and return the corresponding User.

    Returns ``None`` when the cookie is missing, expired, or invalid.
    """
    session_id = request.cookies.get("omakase_session")
    if not session_id:
        return None
    user_id, renewed = authenticate_session(db, session_id)
    if user_id is None:
        return None
    row = db.execute(
        "SELECT id, email, password_hash, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    if renewed:
        request.state.session_cookie_refresh = session_id
    from omakase.plus.avatar import avatar_context

    request.state.account_avatar = avatar_context(db, row["id"])
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
    )


async def require_user(
    request: Request,
    db=Depends(get_db),
) -> User:
    """Like ``get_current_user`` but requires a valid session.

    Safe browser page navigation redirects to the login experience. API-style
    requests retain a machine-readable 401 response.
    """
    user = await get_current_user(request, db)
    if user is None:
        accepts_html = "text/html" in request.headers.get("accept", "").lower()
        if request.method in {"GET", "HEAD"} and accepts_html:
            raise HTTPException(
                status_code=302,
                detail="Sign in required",
                headers={"Location": "/plus/login"},
            )
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"Location": "/plus/login"},
        )
    await enforce_browser_csrf(request)
    return user
