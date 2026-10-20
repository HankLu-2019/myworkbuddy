"""Import Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl)."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatSession, Message, ToolCall

MAX_CONTENT = 20_000


@dataclass
class ParsedTool:
    call_id: str | None
    name: str
    args: dict[str, Any]


@dataclass
class ParsedMessage:
    role: str
    content: str
    at: datetime | None
    tools: list[ParsedTool] = field(default_factory=list)


@dataclass
class ParsedSession:
    session_id: str
    cwd: str | None
    branch: str | None
    messages: list[ParsedMessage]

    @property
    def title(self) -> str:
        first = next((m.content for m in self.messages if m.role == "user" and m.content.strip()), "Imported session")
        return " ".join(first.split())[:120]


def _text(content: Any) -> tuple[str, list[ParsedTool]]:
    if isinstance(content, str):
        return content, []
    texts, tools = [], []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            args = block.get("input") or {}
            tools.append(
                ParsedTool(
                    block.get("id"),
                    str(block.get("name", "?"))[:64],
                    {k: str(v)[:2000] for k, v in args.items()} if isinstance(args, dict) else {},
                )
            )
    return "\n\n".join(t for t in texts if t), tools


def _when(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None


def parse_transcript(data: bytes) -> ParsedSession:
    session_id = cwd = branch = None
    messages: list[ParsedMessage] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") not in ("user", "assistant"):
            continue
        if rec.get("isMeta") or rec.get("isSidechain"):
            continue
        session_id = session_id or rec.get("sessionId")
        cwd, branch = cwd or rec.get("cwd"), branch or rec.get("gitBranch")
        msg = rec.get("message") or {}
        text, tools = _text(msg.get("content"))
        if not text.strip() and not tools:
            continue
        messages.append(ParsedMessage(rec["type"], text[:MAX_CONTENT], _when(rec.get("timestamp")), tools))
    if not session_id or not messages:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Not a Claude Code transcript (no messages found)")
    return ParsedSession(session_id, cwd, branch, messages)


async def import_transcript(db: AsyncSession, owner_id: int, data: bytes) -> tuple[ChatSession, bool]:
    """Returns (session, changed). Re-importing the same transcript only replaces it if it grew."""
    parsed = parse_transcript(data)
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.owner_id == owner_id,
            ChatSession.source == "claude-code",
            ChatSession.external_ref == parsed.session_id,
        )
    )
    if session is not None:
        count = await db.scalar(select(func.count()).where(Message.session_id == session.id))
        if count == len(parsed.messages):
            return session, False
        await db.execute(delete(Message).where(Message.session_id == session.id))
        await db.execute(delete(ToolCall).where(ToolCall.session_id == session.id))
    else:
        session = ChatSession(owner_id=owner_id, source="claude-code", external_ref=parsed.session_id)
        db.add(session)
    session.title = parsed.title
    first_at = next((m.at for m in parsed.messages if m.at), None)
    last_at = next((m.at for m in reversed(parsed.messages) if m.at), None)
    if first_at:
        session.created_at = first_at
    if last_at:
        session.updated_at = session.closed_at = last_at
    await db.flush()
    rows = [
        Message(session_id=session.id, role=m.role, content=m.content, **({"created_at": m.at} if m.at else {}))
        for m in parsed.messages
    ]
    db.add_all(rows)
    await db.flush()
    db.add_all(
        ToolCall(
            session_id=session.id,
            message_id=row.id,
            call_id=t.call_id,
            tool_name=t.name,
            args=t.args,
            status="imported",
        )
        for row, m in zip(rows, parsed.messages, strict=True)
        for t in m.tools
    )
    await db.flush()
    return session, True
