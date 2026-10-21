"""Cursor pagination (rule R13, design 00): `?cursor=<opaque>&limit=1..100` → `{"items": [...], "next_cursor"}`.

Lists are ordered newest first by (created_at, id); the cursor is the last row's pair, so rows added while a
user pages never shift a page.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import status
from sqlalchemy import Select, tuple_

from app.errors import AppError

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def encode(created_at: datetime, row_id: Any) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, row_id = json.loads(raw)
        return datetime.fromisoformat(created), str(row_id)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AppError(status.HTTP_400_BAD_REQUEST, "bad_request", "That page link is no longer valid.") from None


def clamp(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


def page(query: Select, created_col: Any, id_col: Any, cursor: str | None, limit: int) -> Select:
    """Newest first, after the cursor; fetches one extra row to know whether there is a next page."""
    if cursor:
        created, row_id = decode(cursor)
        try:
            key: Any = uuid.UUID(row_id) if id_col.type.python_type is uuid.UUID else int(row_id)
        except ValueError:
            raise AppError(status.HTTP_400_BAD_REQUEST, "bad_request", "That page link is no longer valid.") from None
        query = query.where(tuple_(created_col, id_col) < tuple_(created, key))
    return query.order_by(created_col.desc(), id_col.desc()).limit(limit + 1)


def result(rows: list[Any], limit: int, created: str = "created_at", ident: str = "id") -> tuple[list[Any], str | None]:
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    return rows, encode(getattr(last, created), getattr(last, ident))
