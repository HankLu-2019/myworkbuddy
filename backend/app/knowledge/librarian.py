"""Nightly job: fold each user's new episodes into their private wiki."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic_ai import Agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import LibrarianRunStatus as Status
from app.errors import AppError
from app.knowledge import prompts
from app.knowledge.episodes import EpisodeSummary, render_episode
from app.knowledge.indexer import commit_before_network
from app.knowledge.librarian_guard import clean_output, rejection_reason, unified_diff
from app.knowledge.wiki import LIBRARIAN, find_page, read_page, repo_for, save_page
from app.llm import client as llm
from app.logs import log
from app.models import Episode, LibrarianRun, LibrarianState, Project, User, WikiPage

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
    """Fold new episodes into the user's private pages; records a librarian_runs row. Returns updated paths.

    The caller commits.
    """
    started = datetime.now(UTC)
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

    await commit_before_network(db)  # model_for may create the user's gateway key (rule R1)
    agent = Agent(await llm.model_for(user.id), instructions=prompts.LIBRARIAN)
    updated: list[str] = []
    rejected: list[dict[str, str]] = []
    for path, (page, items) in targets.items():
        notes = "\n\n---\n\n".join(render_episode(s) for s in items[-MAX_EPISODES_PER_PAGE:])
        outcome = await _update_page(db, agent, user, path, page, notes, len(items))
        if outcome is True:
            updated.append(path)
        elif isinstance(outcome, dict):
            rejected.append(outcome)

    if state is None:
        db.add(LibrarianState(user_id=user.id, episodes_until=until))
    else:
        state.episodes_until = until
    status = Status.skipped if not targets else (Status.partial if rejected else Status.ok)
    db.add(
        LibrarianRun(
            user_id=user.id,
            started_at=started,
            finished_at=datetime.now(UTC),
            local_date=started.astimezone(user_tz(user)).date(),
            status=status,
            episodes=len(episodes),
            pages_updated=updated,
            pages_rejected=rejected,
        )
    )
    log.info("librarian.done", user_id=user.id, episodes=len(episodes), pages=updated, rejected=len(rejected))
    return updated


async def _update_page(
    db: AsyncSession, agent: Agent, user: User, path: str, page: WikiPage | None, notes: str, n_items: int
) -> bool | dict[str, str] | None:
    """Rewrite one page. True = saved, dict = rejected (path, reason, diff), None = nothing to do or skipped.

    No transaction is open during the LLM call. If the user edits the page meanwhile, the save is refused
    (409); the page is re-read and rewritten once more, then skipped.
    """
    for attempt in (1, 2):
        if attempt == 2:  # pick up the concurrent edit (or the page someone created meanwhile)
            page = await find_page(db, user.id, "private", path)
            if page is not None:
                await db.refresh(page)
        current = read_page(page) if page else prompts.LIBRARIAN_NEW_PAGES[path]
        base_sha = await repo_for(page.visibility, page.owner_id).last_commit(page.path) if page else None
        await commit_before_network(db)  # rule R1
        prompt = f"Current page ({path}):\n\n{current}\n\nNew session summaries:\n\n{notes}"
        raw = (await llm.run(agent, prompt, user_id=user.id, purpose="librarian_page")).output
        new = clean_output(raw)
        if not new.strip() or new.strip() == current.strip():
            return None
        reason = rejection_reason(current, new, existing_page=page is not None)
        if reason:
            log.warning("librarian.rejected", user_id=user.id, path=path, reason=reason)
            return {"path": path, "reason": reason, "diff": unified_diff(current, new, path)}
        try:
            await save_page(
                db,
                owner=user,
                path=path,
                content=new,
                author=LIBRARIAN,
                message=f"Librarian: update {path} from {n_items} session(s)",
                page=page,
                base_sha=base_sha,
            )
            return True
        except AppError as exc:
            if exc.code != "wiki_conflict":
                raise
            log.info("librarian.page_changed_during_run", user_id=user.id, path=path, attempt=attempt)
    return None


def user_tz(user: User) -> ZoneInfo:
    """The user's timezone (rule R8), falling back to the default, then UTC if the name is invalid."""
    try:
        return ZoneInfo(user.timezone or get_settings().default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def nightly_start(user: User, now: datetime) -> datetime | None:
    """The UTC instant at which today's (user-local) run became due, or None if it isn't due yet.

    Due once local time has reached MWB_LIBRARIAN_LOCAL_HOUR. On a day when that hour doesn't exist (DST spring
    forward) zoneinfo maps it to the next valid instant; on a day when it happens twice, the first one counts.
    """
    tz = user_tz(user)
    local_now = now.astimezone(tz)
    hour = get_settings().librarian_local_hour
    due_local = datetime.combine(local_now.date(), time(hour), tzinfo=tz)
    due = due_local.astimezone(UTC)
    return due if now >= due else None


async def users_due(db: AsyncSession, now: datetime) -> list[int]:
    """Active users whose nightly run is due and hasn't started since it became due."""
    due_ids = []
    for user in await db.scalars(select(User).where(User.is_active)):
        start = nightly_start(user, now)
        if start is None:
            continue
        ran = await db.scalar(
            select(LibrarianRun.id).where(LibrarianRun.user_id == user.id, LibrarianRun.started_at >= start).limit(1)
        )
        if ran is None:
            due_ids.append(user.id)
    return due_ids
