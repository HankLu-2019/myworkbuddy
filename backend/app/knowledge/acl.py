"""Single place that decides who can see or change knowledge.

Everything is private to its owner unless explicitly published; public items are visible to the whole team.
"""

from typing import Literal, Protocol

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, or_

from app.models import User

Visibility = Literal["private", "public"]


class Owned(Protocol):
    owner_id: int
    visibility: str


def visible_filter(model: type, user: User) -> ColumnElement[bool]:
    return or_(model.visibility == "public", model.owner_id == user.id)


def can_view(obj: Owned, user: User) -> bool:
    return obj.visibility == "public" or obj.owner_id == user.id


def can_edit(obj: Owned, user: User) -> bool:
    return obj.owner_id == user.id or (obj.visibility == "public" and user.is_admin)


def can_change_visibility(obj: Owned, user: User, target: Visibility) -> bool:
    if obj.owner_id == user.id:
        return True
    # Admins can take content down from the team space, but never publish someone else's private work.
    return user.is_admin and obj.visibility == "public" and target == "private"


def ensure_view(obj: Owned | None, user: User) -> None:
    # 404 instead of 403 so private items don't reveal that they exist.
    if obj is None or not can_view(obj, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def ensure_edit(obj: Owned, user: User) -> None:
    if not can_edit(obj, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can change this")
