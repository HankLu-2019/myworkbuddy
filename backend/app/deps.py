import secrets
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import resolve_session
from app.config import get_settings
from app.db import get_db
from app.errors import AppError
from app.models import AuthSession, User

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

DB = Annotated[AsyncSession, Depends(get_db)]


@dataclass
class Auth:
    user: User
    session: AuthSession


async def get_auth(request: Request, db: DB) -> Auth:
    token = request.cookies.get(get_settings().session_cookie)
    resolved = await resolve_session(db, token) if token else None
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    session, user = resolved
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent, session.csrf_token):
            raise AppError(status.HTTP_403_FORBIDDEN, "csrf_invalid", "Missing or invalid CSRF token")
    structlog.contextvars.bind_contextvars(user_id=user.id)
    return Auth(user=user, session=session)


async def get_admin(auth: Annotated[Auth, Depends(get_auth)]) -> Auth:
    if not auth.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return auth


CurrentAuth = Annotated[Auth, Depends(get_auth)]
AdminAuth = Annotated[Auth, Depends(get_admin)]


def client_ip(request: Request) -> str:
    # The proxy overwrites X-Forwarded-For with the real peer (nginx.conf), so this can't be spoofed through it.
    return (request.client.host if request.client else "unknown")[:64]
