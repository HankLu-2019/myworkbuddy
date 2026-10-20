import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import prompts
from app.knowledge.chunking import ChunkIn
from app.knowledge.indexer import upsert_document
from app.llm.models import get_chat_model
from app.logs import log
from app.models import ChatSession, Episode, Message, ToolCall

TRANSCRIPT_BUDGET = 60_000
IDLE_BEFORE_SUMMARY = timedelta(hours=1)


class EpisodeSummary(BaseModel):
    title: str = Field(description="Short title of what the session was about")
    goal: str
    decisions: list[str] = []
    repos: list[str] = Field(default=[], description="Repository or project names touched")
    files: list[str] = []
    tickets: list[str] = []
    outcome: str
    open_items: list[str] = []
    learnings: list[str] = Field(default=[], description="Reusable lessons, preferences or conventions")


def render_episode(s: EpisodeSummary) -> str:
    def section(name: str, items: list[str]) -> str:
        return f"## {name}\n" + "\n".join(f"- {i}" for i in items) + "\n" if items else ""

    return (
        f"# {s.title}\n\n**Goal:** {s.goal}\n\n**Outcome:** {s.outcome}\n\n"
        + section("Decisions", s.decisions)
        + section("Repos", s.repos)
        + section("Files", s.files)
        + section("Tickets", s.tickets)
        + section("Open items", s.open_items)
        + section("Learnings", s.learnings)
    )


async def _transcript(db: AsyncSession, session_id: uuid.UUID) -> tuple[str, int]:
    messages = list(await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id)))
    tools = await db.execute(
        select(ToolCall.message_id, ToolCall.tool_name, ToolCall.args).where(ToolCall.session_id == session_id)
    )
    by_msg: dict[int | None, list[str]] = {}
    for message_id, name, args in tools:
        by_msg.setdefault(message_id, []).append(f"[tool {name} {str(args)[:300]}]")
    lines = []
    for m in messages:
        lines.append(f"{m.role.upper()}: {m.content[:4000]}")
        lines.extend(by_msg.get(m.id, []))
    text = "\n".join(lines)
    if len(text) > TRANSCRIPT_BUDGET:  # keep the start (the ask) and the end (the outcome)
        half = TRANSCRIPT_BUDGET // 2
        text = text[:half] + "\n...[middle omitted]...\n" + text[-half:]
    return text, len(messages)


async def build_episode(db: AsyncSession, session_id: uuid.UUID) -> Episode | None:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return None
    transcript, count = await _transcript(db, session_id)
    episode = await db.scalar(select(Episode).where(Episode.session_id == session_id))
    if count == 0 or (episode is not None and episode.message_count == count):
        return episode
    agent = Agent(get_chat_model(), output_type=EpisodeSummary, instructions=prompts.EPISODE)
    summary = (await agent.run(f"Session title: {session.title}\nSource: {session.source}\n\n{transcript}")).output
    if episode is None:
        episode = Episode(session_id=session_id, owner_id=session.owner_id, visibility=session.visibility, summary={})
        db.add(episode)
    episode.summary, episode.message_count = summary.model_dump(), count
    doc = await upsert_document(
        db,
        owner_id=session.owner_id,
        source_type="episode",
        source_ref=f"session:{session_id}",
        title=summary.title,
        chunks=[ChunkIn(render_episode(summary), {"session_id": str(session_id)})],
        visibility=session.visibility,
        project_id=session.project_id,
        meta={"session_id": str(session_id), "source": session.source},
    )
    episode.document_id = doc.id
    await db.flush()
    log.info("episode.built", session_id=str(session_id), messages=count)
    return episode


async def sessions_needing_episodes(db: AsyncSession, limit: int = 50) -> list[uuid.UUID]:
    """Sessions idle for an hour whose episode is missing or older than the latest message."""
    cutoff = datetime.now(UTC) - IDLE_BEFORE_SUMMARY
    msg_count = select(func.count()).where(Message.session_id == ChatSession.id).scalar_subquery()
    rows = await db.scalars(
        select(ChatSession.id)
        .outerjoin(Episode, Episode.session_id == ChatSession.id)
        .where(ChatSession.updated_at < cutoff, msg_count > 0)
        .where((Episode.id.is_(None)) | (Episode.message_count < msg_count))
        .order_by(ChatSession.updated_at)
        .limit(limit)
    )
    return list(rows)
