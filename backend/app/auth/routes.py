from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete

from app.audit.service import audit
from app.auth.service import (
    authenticate,
    create_session,
    hash_password,
    is_rate_limited,
    normalize_username,
    record_failed_login,
    revoke_user_sessions,
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
    role: str
    is_active: bool

    @classmethod
    def of(cls, u: User) -> "UserOut":
        return cls(
            id=u.id, username=u.username, email=u.email, display_name=u.display_name, role=u.role, is_active=u.is_active
        )


class MeOut(BaseModel):
    user: UserOut
    csrf_token: str


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
    key = f"{normalize_username(body.username)}|{ip}"
    if await is_rate_limited(db, key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again later.")
    user = await authenticate(db, body.username, body.password)
    if user is None:
        await audit(db, "login.failed", user_id=None, detail={"username": normalize_username(body.username)}, ip=ip)
        await record_failed_login(db, key)
        raise AppError(status.HTTP_401_UNAUTHORIZED, "invalid_credentials", "Wrong username or password")
    token, session = await create_session(db, user, key)
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


@router.post("/password", status_code=204)
async def change_password(body: PasswordChangeIn, auth: CurrentAuth, db: DB) -> None:
    if not verify_password(auth.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    validate_password(body.new_password)
    user = await db.get(User, auth.user.id)
    user.password_hash = hash_password(body.new_password)
    await revoke_user_sessions(db, user.id)
    await audit(db, "password.change", user_id=user.id)
    await db.commit()


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Password must be at least 10 characters")
