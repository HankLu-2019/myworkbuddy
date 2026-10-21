from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit.service import audit
from app.auth.routes import UserOut
from app.auth.service import generate_password, hash_password, normalize_username, revoke_user_sessions
from app.deps import DB, AdminAuth, client_ip
from app.errors import AppError
from app.models import User
from app.users.offboarding import offboard, plan

router = APIRouter(prefix="/api/admin/users", tags=["admin"])

USERNAME_PATTERN = r"^[a-zA-Z0-9._-]{2,64}$"


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # passwords are generated, never chosen by the admin

    username: str = Field(pattern=USERNAME_PATTERN)
    email: EmailStr | None = None
    display_name: str | None = None
    role: Literal["admin", "member"] = "member"


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")  # use POST .../reset-password

    email: EmailStr | None = None
    display_name: str | None = None
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None


class UserWithPassword(UserOut):
    """Returned once, on create and on reset. The password is never stored or shown again."""

    initial_password: str


class OffboardingPreview(BaseModel):
    transfer_to: int
    transfer: dict[str, int]
    delete: dict[str, int]
    duplicate_projects: list[int]


async def _user(db: DB, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


async def _transfer_target(db: DB, user: User, transfer_to: int, auth_user: User) -> User:
    if user.id == auth_user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't delete yourself")
    target = await db.get(User, transfer_to)
    if target is None or not target.is_admin or not target.is_active or target.id == user.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Team items must go to another active admin")
    return target


@router.get("", response_model=list[UserOut])
async def list_users(auth: AdminAuth, db: DB) -> list[UserOut]:
    users = await db.scalars(select(User).order_by(User.username))
    return [UserOut.of(u) for u in users]


@router.post("", response_model=UserWithPassword, status_code=201)
async def create_user(body: UserCreate, auth: AdminAuth, db: DB) -> UserWithPassword:
    password = generate_password()
    user = User(
        username=normalize_username(body.username),
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        password_hash=hash_password(password),
        must_change_password=True,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already exists") from None
    await audit(db, "user.create", user_id=auth.user.id, target_type="user", target_id=user.id)
    await db.commit()
    return UserWithPassword(**UserOut.of(user).model_dump(), initial_password=password)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, auth: AdminAuth, db: DB) -> UserOut:
    user = await _user(db, user_id)
    if user.id == auth.user.id and (body.is_active is False or body.role == "member"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't disable or demote yourself")
    changes = body.model_dump(exclude_unset=True)
    for name, value in changes.items():
        setattr(user, name, value)
    if body.is_active is False:
        await revoke_user_sessions(db, user.id)
    await audit(db, "user.update", user_id=auth.user.id, target_type="user", target_id=user.id, detail=changes)
    await db.commit()
    return UserOut.of(user)


@router.post("/{user_id}/reset-password", response_model=UserWithPassword)
async def reset_password(user_id: int, auth: AdminAuth, db: DB) -> UserWithPassword:
    """New one-time password; signs the user out everywhere and makes them choose a new one at next sign-in."""
    user = await _user(db, user_id)
    password = generate_password()
    user.password_hash = hash_password(password)
    user.must_change_password = True
    await revoke_user_sessions(db, user.id)
    await audit(db, "user.reset_password", user_id=auth.user.id, target_type="user", target_id=user.id)
    await db.commit()
    return UserWithPassword(**UserOut.of(user).model_dump(), initial_password=password)


@router.get("/{user_id}/offboarding-preview", response_model=OffboardingPreview)
async def offboarding_preview(user_id: int, transfer_to: int, auth: AdminAuth, db: DB) -> OffboardingPreview:
    user = await _user(db, user_id)
    target = await _transfer_target(db, user, transfer_to, auth.user)
    p = await plan(db, user, target)
    return OffboardingPreview(
        transfer_to=target.id, transfer=p.transfer, delete=p.delete, duplicate_projects=p.kept_duplicate_projects
    )


@router.delete("/{user_id}", response_model=OffboardingPreview)
async def delete_user(user_id: int, transfer_to: int, auth: AdminAuth, db: DB, request: Request) -> OffboardingPreview:
    """Offboard: team items go to `transfer_to` (an active admin), everything else is deleted (design 01 §H11)."""
    from app.tasks.app import defer_cleanup_after_offboarding

    user = await _user(db, user_id)
    target = await _transfer_target(db, user, transfer_to, auth.user)
    try:
        result = await offboard(db, user, target, actor_id=auth.user.id, ip=client_ip(request))
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT, "conflict", "Some items could not be transferred; nothing changed"
        ) from exc
    await db.commit()
    await defer_cleanup_after_offboarding(user_id, result.deleted_project_ids)
    return OffboardingPreview(
        transfer_to=target.id,
        transfer=result.transfer,
        delete=result.delete,
        duplicate_projects=result.kept_duplicate_projects,
    )
