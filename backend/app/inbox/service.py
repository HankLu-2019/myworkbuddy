"""Inbox items (design 02 §5). Callers add items inside their own transaction and commit."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InboxItem


def add_item(
    db: AsyncSession,
    owner_id: int,
    *,
    kind: str,
    severity: str,
    title: str,
    body_md: str = "",
    link: str | None = None,
    run_id: uuid.UUID | None = None,
) -> InboxItem:
    if link is not None and (not link.startswith("/") or link.startswith(("//", "/\\"))):
        raise ValueError("inbox links are in-app paths")  # never an external URL (02 §5)
    item = InboxItem(
        owner_id=owner_id, kind=kind, severity=severity, title=title[:200], body_md=body_md, link=link, run_id=run_id
    )
    db.add(item)
    return item
