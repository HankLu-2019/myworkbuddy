"""Single sign-on routes (design 08 §F11). The provider redirects the browser here, so these are plain GETs that
end in a redirect: to the provider, back into the app, or to the sign-in page with `?sso_error=<code>`.

Matching (in one short transaction, after the provider has answered): the account whose `oidc_sub` is the token's
`sub`; otherwise, once, the active account with the token's verified email and no `sub` yet, which records it;
otherwise a new member when `MWB_OIDC_AUTO_CREATE` is on. Deactivated accounts are refused. Every outcome is audited.
"""

import secrets
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit
from app.auth import oidc
from app.auth.routes import _set_cookie
from app.auth.service import create_session, hash_password, login_keys
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import client_ip
from app.errors import AppError
from app.logs import log
from app.models import User

router = APIRouter(prefix="/api/auth/oidc", tags=["auth"])


class OidcConfig(BaseModel):
    enabled: bool
    name: str
    local_login: Literal["all", "admins"]


@router.get("/config", response_model=OidcConfig)
async def config() -> OidcConfig:
    """For the sign-in page, before anyone is signed in."""
    s = get_settings()
    return OidcConfig(enabled=oidc.enabled(), name=s.oidc_name, local_login=s.local_login)


def _flow_cookie(response: RedirectResponse, value: str | None) -> None:
    s = get_settings()
    if value is None:
        response.delete_cookie(oidc.COOKIE, path=oidc.COOKIE_PATH, secure=s.cookie_secure, httponly=True,
                               samesite="lax")  # fmt: skip
    else:
        response.set_cookie(oidc.COOKIE, value, max_age=oidc.FLOW_TTL_S, path=oidc.COOKIE_PATH, httponly=True,
                            secure=s.cookie_secure, samesite="lax")  # fmt: skip


def _to_login(code: str) -> RedirectResponse:
    response = RedirectResponse(f"/login?sso_error={code}", status_code=302)
    _flow_cookie(response, None)
    return response


@router.get("/start")
async def start(next: str | None = None) -> RedirectResponse:
    if not oidc.enabled():
        raise AppError(404, "not_found", "Single sign-on isn't set up")
    try:
        flow = await oidc.start(next)
    except oidc.OidcError as exc:
        log.warning("oidc.start_failed", code=exc.code, detail=exc.detail)
        return _to_login(exc.code)
    response = RedirectResponse(flow.url, status_code=302)
    _flow_cookie(response, flow.cookie)
    return response


def _username_base(claims: dict) -> str:
    raw = str(claims.get("preferred_username") or str(claims.get("email") or "").split("@")[0] or "user")
    name = "".join(ch for ch in raw.lower() if ch.isalnum() or ch in "._-")[:56]
    return name if len(name) >= 2 else "user"


async def _free_username(db: AsyncSession, base: str) -> str:
    name = base
    while await db.scalar(select(User.id).where(User.username == name)) is not None:
        name = f"{base}-{secrets.randbelow(10_000):04d}"
    return name


async def _match(db: AsyncSession, claims: dict, ip: str) -> tuple[User | None, str | None]:
    """The account to sign in, or why not."""
    sub = str(claims["sub"])
    user = await db.scalar(select(User).where(User.oidc_sub == sub))
    email = str(claims.get("email") or "").strip()
    if user is None and email and claims.get("email_verified") is True:
        found = list(
            await db.scalars(select(User).where(func.lower(User.email) == email.lower(), User.oidc_sub.is_(None)))
        )
        if len(found) > 1:
            return None, "ambiguous"
        if found:
            user = found[0]
            user.oidc_sub = sub
            await audit(db, "user.oidc_linked", user_id=user.id, target_type="user", target_id=user.id, ip=ip)
    if user is None:
        if not get_settings().oidc_auto_create:
            return None, "no_account"
        user = User(
            username=await _free_username(db, _username_base(claims)), role="member", oidc_sub=sub,
            email=email if email and claims.get("email_verified") is True else None,
            display_name=str(claims.get("name") or "")[:128] or None,
            password_hash=hash_password(secrets.token_urlsafe(32)),  # no local password until an admin sets one
        )  # fmt: skip
        db.add(user)
        await db.flush()
        await audit(db, "user.create", user_id=user.id, target_type="user", target_id=user.id,
                    detail={"via": "oidc"}, ip=ip)  # fmt: skip
    if not user.is_active:
        return None, "inactive"
    return user, None


@router.get("/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    if not oidc.enabled():
        raise AppError(404, "not_found", "Single sign-on isn't set up")
    ip = client_ip(request)
    if error:
        problem: str | None = "denied"
        detail = f"the provider answered error={error[:50]}"
        claims, next_path = None, "/"
    else:
        try:
            claims, next_path = await oidc.finish(code, state, request.cookies.get(oidc.COOKIE))  # network (R1)
            problem, detail = None, ""
        except oidc.OidcError as exc:
            claims, next_path, problem, detail = None, "/", exc.code, exc.detail
    async with get_sessionmaker()() as db:
        user = None
        if claims is not None:
            user, problem = await _match(db, claims, ip)
            detail = problem or ""
        if user is None:
            await audit(db, "login.failed", user_id=None, detail={"method": "oidc", "reason": problem}, ip=ip)
            await db.commit()
            log.warning("oidc.login_refused", code=problem, detail=detail)
            return _to_login(problem or "denied")
        token, session = await create_session(db, user, login_keys(user.username, ip), ip=ip,
                                              user_agent=request.headers.get("user-agent"))  # fmt: skip
        await audit(db, "login", user_id=user.id, detail={"method": "oidc"}, ip=ip)
        await db.commit()
    response = RedirectResponse(next_path, status_code=302)
    _set_cookie(response, token)
    _flow_cookie(response, None)
    del session
    return response
