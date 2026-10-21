from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.audit.service import audit
from app.auth.service import (
    authenticate,
    create_session,
    hash_password,
    is_rate_limited,
    login_keys,
    normalize_username,
    record_failed_login,
    revoke_user_sessions,
    session_alive_after,
    verify_password,
)
from app.config import get_settings
from app.deps import DB, CurrentAuth, client_ip
from app.errors import AppError
from app.models import AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    display_name: str | None
    role: Literal["admin", "member"]
    is_active: bool
    must_change_password: bool = False
    last_login_at: datetime | None = None
    llm_budget_usd: float | None = None  # None: the default MWB_DEFAULT_USER_BUDGET_USD applies
    timezone: str | None = None
    timezone_confirmed: bool = False

    @classmethod
    def of(cls, u: User) -> "UserOut":
        return cls(
            id=u.id,
            username=u.username,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            is_active=u.is_active,
            must_change_password=bool(u.must_change_password),
            last_login_at=u.last_login_at,
            llm_budget_usd=float(u.llm_budget_usd) if u.llm_budget_usd is not None else None,
            timezone=u.timezone,
            timezone_confirmed=bool(u.timezone_confirmed),
        )


class MeOut(BaseModel):
    user: UserOut
    csrf_token: str


class ProfileIn(BaseModel):
    """Settings → Profile (07-ux §4.12). Omitted fields stay as they are; an empty email clears it."""

    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=255, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    timezone: str | None = Field(default=None, max_length=64)


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


def _set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie,
        token,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        max_age=s.session_ttl_hours * 3600,
        path="/",
    )


@router.post("/login", response_model=MeOut)
async def login(body: LoginIn, request: Request, response: Response, db: DB) -> MeOut:
    ip = client_ip(request)
    keys = login_keys(body.username, ip)
    if await is_rate_limited(db, keys):
        # Same answer for every limit, so it doesn't reveal which key tripped (design 01 §H11).
        raise AppError(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited", "Too many attempts. Try again in 15 minutes.")
    user = await authenticate(db, body.username, body.password)
    if user is None:
        await audit(db, "login.failed", user_id=None, detail={"username": normalize_username(body.username)}, ip=ip)
        await record_failed_login(db, keys)
        raise AppError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Wrong username or password")
    token, session = await create_session(db, user, keys, ip=ip, user_agent=request.headers.get("user-agent"))
    await audit(db, "login", user_id=user.id, ip=ip)
    await db.commit()
    _set_cookie(response, token)
    return MeOut(user=UserOut.of(user), csrf_token=session.csrf_token)


@router.post("/logout", status_code=204)
async def logout(auth: CurrentAuth, response: Response, db: DB) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.id == auth.session.id))
    await audit(db, "logout", user_id=auth.user.id)
    await db.commit()
    s = get_settings()
    response.delete_cookie(s.session_cookie, path="/", secure=s.cookie_secure, httponly=True, samesite="lax")


@router.get("/me", response_model=MeOut)
async def me(auth: CurrentAuth) -> MeOut:
    return MeOut(user=UserOut.of(auth.user), csrf_token=auth.session.csrf_token)


@router.patch("/me", response_model=UserOut)
async def update_profile(body: ProfileIn, auth: CurrentAuth, db: DB) -> UserOut:
    user = auth.user
    changed = body.model_dump(exclude_unset=True)
    if "timezone" in changed:
        try:
            ZoneInfo(body.timezone or "")
        except (ZoneInfoNotFoundError, ValueError):
            raise AppError(422, "validation_error", f"Unknown time zone {body.timezone!r}") from None
        user.timezone = body.timezone
        user.timezone_confirmed = True
    if "display_name" in changed:
        user.display_name = (body.display_name or "").strip() or None
    if "email" in changed:
        user.email = (body.email or "").strip() or None
    await audit(db, "profile.update", user_id=user.id, detail={"fields": sorted(changed)})
    await db.commit()
    return UserOut.of(user)


@router.post("/password", status_code=204)
async def change_password(body: PasswordChangeIn, auth: CurrentAuth, db: DB) -> None:
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    validate_password(body.new_password)
    if verify_password(auth.user.password_hash, body.new_password):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a password different from the current one")
    user = await db.get(User, auth.user.id)
    forced = bool(user.must_change_password)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    # A normal change signs out everywhere (07-ux §4 Settings). Replacing a one-time password keeps this
    # sign-in, so a new user goes straight on to Getting started (07-ux §5).
    await revoke_user_sessions(db, user.id, keep=auth.session.id if forced else None)
    await audit(db, "password.change", user_id=user.id, detail={"initial": forced})
    await db.commit()


class SessionOut(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(auth: CurrentAuth, db: DB) -> list[SessionOut]:
    """The caller's live sessions, most recently used first."""
    now = datetime.now(UTC)
    rows = await db.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == auth.user.id,
            AuthSession.expires_at > now,
            AuthSession.last_seen_at > session_alive_after(now),
        )
        .order_by(AuthSession.last_seen_at.desc())
        .limit(100)
    )
    return [
        SessionOut(
            id=r.id,
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            expires_at=r.expires_at,
            ip=r.ip,
            user_agent=r.user_agent,
            current=r.id == auth.session.id,
        )
        for r in rows
    ]


@router.post("/sessions/sign-out-others")
async def sign_out_others(auth: CurrentAuth, db: DB) -> dict[str, int]:
    removed = await revoke_user_sessions(db, auth.user.id, keep=auth.session.id)
    await audit(db, "sessions.sign_out_others", user_id=auth.user.id, detail={"sessions": removed})
    await db.commit()
    return {"signed_out": removed}


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password must be at least 10 characters")
