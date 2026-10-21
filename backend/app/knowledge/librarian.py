"""Nightly job: fold each user's new episodes into their private wiki."""

from datetime import UTC, datetime, timedelta

from pydantic_ai import Agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import prompts
from app.knowledge.episodes import EpisodeSummary, render_episode
from app.knowledge.indexer import commit_before_network
from app.knowledge.wiki import LIBRARIAN, find_page, read_page, save_page
from app.llm.models import get_chat_model
from app.logs import log
from app.models import Episode, LibrarianState, Project, User, WikiPage

MAX_EPISODES_PER_PAGE = 20
FIRST_RUN_LOOKBACK = timedelta(days=1)


def _key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


async def _project_cards(db: AsyncSession, user: User) -> dict[str, WikiPage]:
    """The user's own project cards, keyed loosely by project name so 'Data-Pipeline' matches 'datapipeline'."""
    rows = await db.execute(
        select(Project.name, WikiPage)
        .join(WikiPage, WikiPage.id == Project.card_page_id)
        .where(Project.owner_id == user.id, WikiPage.owner_id == user.id, WikiPage.visibility == "private")
    )
    return {_key(name): page for name, page in rows}


async def update_user_wiki(db: AsyncSession, user: User) -> list[str]:
    # Watermark = when the previous run *queried* episodes, so episodes built while it ran are picked up next time.
    state = await db.get(LibrarianState, user.id)
    since = state.episodes_until if state else datetime.now(UTC) - FIRST_RUN_LOOKBACK
    until = datetime.now(UTC)
    episodes = list(
        await db.scalars(
            select(Episode)
            .where(Episode.owner_id == user.id, Episode.updated_at > since, Episode.updated_at <= until)
            .order_by(Episode.updated_at)
        )
    )
    summaries = [EpisodeSummary.model_validate(e.summary) for e in episodes]

    targets: dict[str, tuple[WikiPage | None, list[EpisodeSummary]]] = {}
    cards = await _project_cards(db, user) if summaries else {}
    for s in summaries:
        for path in ["me/how-i-work.md", *(["decisions.md"] if s.decisions else [])]:
            targets.setdefault(path, (await find_page(db, user.id, "private", path), []))[1].append(s)
        # Only projects that have a card; don't spawn a page for every repo name a session mentions.
        for repo in s.repos[:5]:
            if card := cards.get(_key(repo)):
                targets.setdefault(card.path, (card, []))[1].append(s)

    agent = Agent(get_chat_model(), instructions=prompts.LIBRARIAN)
    updated = []
    for path, (page, items) in targets.items():
        current = read_page(page) if page else prompts.LIBRARIAN_NEW_PAGES[path]
        notes = "\n\n---\n\n".join(render_episode(s) for s in items[-MAX_EPISODES_PER_PAGE:])
        await commit_before_network(db)  # rule R1: no transaction open during the LLM call
        new = (await agent.run(f"Current page ({path}):\n\n{current}\n\nNew session summaries:\n\n{notes}")).output
        if new.strip() and new.strip() != current.strip():
            await save_page(
                db,
                owner=user,
                path=path,
                content=new.strip() + "\n",
                author=LIBRARIAN,
                message=f"Librarian: update {path} from {len(items)} session(s)",
                page=page,
            )
            updated.append(path)

    if state is None:
        db.add(LibrarianState(user_id=user.id, episodes_until=until))
    else:
        state.episodes_until = until
    log.info("librarian.done", user_id=user.id, episodes=len(episodes), pages=updated)
    return updated
