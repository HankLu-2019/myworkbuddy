"""Inbox API (design 02 §7): the caller's own items only."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app import pagination
from app.audit.service import audit
from app.deps import DB, CurrentAuth
from app.errors import AppError
from app.models import InboxItem

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


class InboxItemOut(BaseModel):
    id: int
    kind: Literal["run", "alert", "recovered", "system", "credential", "proposal", "plan", "job"]
    severity: Literal["info", "warning", "alert"]
    title: str
    body_md: str
    link: str | None
    run_id: uuid.UUID | None
    read_at: datetime | None
    created_at: datetime


class InboxPage(BaseModel):
    items: list[InboxItemOut]
    next_cursor: str | None


class UnreadCount(BaseModel):
    unread: int


class Marked(BaseModel):
    marked: int
    ids: list[int] = []  # what changed, so the UI can undo


class Ids(BaseModel):
    ids: list[int] = Field(max_length=500)


@router.get("", response_model=InboxPage)
async def list_inbox(
    auth: CurrentAuth,
    db: DB,
    unread: bool = False,
    kind: Literal["alert"] | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> InboxPage:
    q = select(InboxItem).where(InboxItem.owner_id == auth.user.id)
    if unread:
        q = q.where(InboxItem.read_at.is_(None))
    if kind == "alert":
        q = q.where(InboxItem.kind == "alert")
    rows = list(await db.scalars(pagination.page(q, InboxItem.created_at, InboxItem.id, cursor, limit)))
    items, next_cursor = pagination.result(rows, limit)
    return InboxPage(
        items=[InboxItemOut.model_validate(i, from_attributes=True) for i in items], next_cursor=next_cursor
    )


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(auth: CurrentAuth, db: DB) -> UnreadCount:
    n = await db.scalar(
        select(func.count())
        .select_from(InboxItem)
        .where(InboxItem.owner_id == auth.user.id, InboxItem.read_at.is_(None))
    )
    return UnreadCount(unread=int(n or 0))


@router.post("/{item_id}/read", response_model=InboxItemOut)
async def mark_read(item_id: int, auth: CurrentAuth, db: DB) -> InboxItemOut:
    item = await db.scalar(select(InboxItem).where(InboxItem.id == item_id, InboxItem.owner_id == auth.user.id))
    if item is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Inbox item not found")
    if item.read_at is None:
        item.read_at = func.now()
        await audit(db, "inbox.read", user_id=auth.user.id, target_type="inbox_item", target_id=item.id)
        await db.commit()
        await db.refresh(item)
    return InboxItemOut.model_validate(item, from_attributes=True)


@router.post("/read-all", response_model=Marked)
async def mark_all_read(auth: CurrentAuth, db: DB) -> Marked:
    ids = list(
        await db.scalars(
            update(InboxItem)
            .where(InboxItem.owner_id == auth.user.id, InboxItem.read_at.is_(None))
            .values(read_at=func.now())
            .returning(InboxItem.id)
        )
    )
    await audit(db, "inbox.read_all", user_id=auth.user.id, detail={"marked": len(ids)})
    await db.commit()
    return Marked(marked=len(ids), ids=sorted(ids))


@router.post("/unread", response_model=Marked)
async def mark_unread(body: Ids, auth: CurrentAuth, db: DB) -> Marked:
    """Mark items unread again ("Mark unread", and undo of "Mark all read")."""
    ids = list(
        await db.scalars(
            update(InboxItem)
            .where(InboxItem.owner_id == auth.user.id, InboxItem.id.in_(body.ids), InboxItem.read_at.is_not(None))
            .values(read_at=None)
            .returning(InboxItem.id)
        )
    )
    await audit(db, "inbox.unread", user_id=auth.user.id, detail={"marked": len(ids)})
    await db.commit()
    return Marked(marked=len(ids), ids=sorted(ids))


@router.delete("/{item_id}", status_code=204)
async def dismiss(item_id: int, auth: CurrentAuth, db: DB) -> None:
    """Dismiss (delete) one item. The UI waits out its undo toast before calling this."""
    item = await db.scalar(select(InboxItem).where(InboxItem.id == item_id, InboxItem.owner_id == auth.user.id))
    if item is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Inbox item not found")
    await db.delete(item)
    await audit(db, "inbox.dismiss", user_id=auth.user.id, target_type="inbox_item", target_id=item_id)
    await db.commit()
