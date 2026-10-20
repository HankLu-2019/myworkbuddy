from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit.service import audit
from app.auth.routes import UserOut, validate_password
from app.auth.service import hash_password, normalize_username, revoke_user_sessions
from app.deps import DB, AdminAuth
from app.models import User

router = APIRouter(prefix="/api/admin/users", tags=["admin"])

USERNAME_PATTERN = r"^[a-zA-Z0-9._-]{2,64}$"


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    password: str
    email: EmailStr | None = None
    display_name: str | None = None
    role: Literal["admin", "member"] = "member"


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    display_name: str | None = None
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None
    password: str | None = None


@router.get("", response_model=list[UserOut])
async def list_users(auth: AdminAuth, db: DB) -> list[UserOut]:
    users = await db.scalars(select(User).order_by(User.username))
    return [UserOut.of(u) for u in users]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, auth: AdminAuth, db: DB) -> UserOut:
    validate_password(body.password)
    user = User(
        username=normalize_username(body.username),
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists") from None
    await audit(db, "user.create", user_id=auth.user.id, target_type="user", target_id=user.id)
    await db.commit()
    return UserOut.of(user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, auth: AdminAuth, db: DB) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == auth.user.id and (body.is_active is False or body.role == "member"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable or demote yourself")
    changes = body.model_dump(exclude_unset=True, exclude={"password"})
    for field, value in changes.items():
        setattr(user, field, value)
    if body.password is not None:
        validate_password(body.password)
        user.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    if body.password is not None or body.is_active is False:
        await revoke_user_sessions(db, user.id)
    await audit(
        db,
        "user.update",
        user_id=auth.user.id,
        target_type="user",
        target_id=user.id,
        detail={k: v if k != "password" else "reset" for k, v in changes.items()},
    )
    await db.commit()
    return UserOut.of(user)
