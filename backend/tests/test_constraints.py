"""CHECK constraints keep enum columns valid at the database (rule R9, migration 0003)."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import get_engine

BAD_VALUES = [
    "UPDATE users SET role = 'owner'",
    "UPDATE chat_sessions SET visibility = 'team'",
    "UPDATE chat_sessions SET source = 'slack'",
    "UPDATE messages SET status = 'done'",
]


@pytest.mark.parametrize("stmt", BAD_VALUES)
async def test_invalid_enum_values_are_rejected(client_for, stmt: str) -> None:
    c = await client_for("jane")
    sid = (await c.post("/api/chat/sessions", json={})).json()["id"]
    async with get_engine().begin() as conn:
        await conn.execute(
            text("INSERT INTO messages (session_id, role, content) VALUES (:s, 'user', 'hi')"), {"s": sid}
        )
    with pytest.raises(IntegrityError):
        async with get_engine().begin() as conn:
            await conn.execute(text(stmt))
