import uuid
from datetime import UTC, datetime

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued

from app import maintenance
from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import LibrarianRunStatus
from app.knowledge.episodes import build_episode, sessions_needing_episodes
from app.knowledge.indexer import embed_missing
from app.knowledge.ingest.repos import ingest_project, remove_cache, sweep_orphan_caches
from app.knowledge.librarian import update_user_wiki, user_tz, users_due
from app.knowledge.reconcile import reconcile_wiki
from app.logs import log, setup_logging
from app.models import LibrarianRun, User

setup_logging(get_settings().log_level)

app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=get_settings().database_url))


async def defer_once(
    task: procrastinate.tasks.Task, queueing_lock: str, *, lock: str | None = None, **kwargs: object
) -> bool:
    """Queue the task unless an identical one is already waiting. Returns whether it was queued.

    `queueing_lock` only de-duplicates waiting jobs; `lock` (rule R4) keeps jobs for one entity from running at
    the same time, e.g. a re-ingest queued while the previous ingest still runs waits for it.
    """
    try:
        await task.configure(queueing_lock=queueing_lock, lock=lock or queueing_lock).defer_async(**kwargs)
        return True
    except AlreadyEnqueued:
        return False


def project_lock(project_id: int) -> str:
    return f"project:{project_id}"


async def defer_ingest(project_id: int, force: bool = False) -> bool:
    return await defer_once(
        ingest_project_task, f"ingest:{project_id}", lock=project_lock(project_id), project_id=project_id, force=force
    )


@app.task(name="ingest_project", retry=False)
async def ingest_project_task(project_id: int, force: bool = False) -> None:
    structlog.contextvars.bind_contextvars(task="ingest_project", project_id=project_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, project_id, force=force)


@app.task(name="cleanup_project_cache")
async def cleanup_project_cache(project_id: int) -> None:
    """Remove a deleted project's checkout. Runs under the project lock, so never during its ingest."""
    if remove_cache(project_id):
        log.info("repo_cache.removed", project_id=project_id)


@app.periodic(cron="17 * * * *")
@app.task(name="maintenance_hourly", queueing_lock="maintenance_hourly")
async def maintenance_hourly(timestamp: int) -> dict[str, int]:
    """Old login attempts, expired or idle sessions, finished jobs, old audit rows, /data/tmp (design 01 §H11)."""
    counts = await maintenance.prune_database()
    await app.job_manager.delete_old_jobs(
        nb_hours=maintenance.JOBS_KEEP_HOURS, include_failed=True, include_cancelled=True, include_aborted=True
    )
    counts["tmp_entries"] = maintenance.prune_tmp()
    log.info("maintenance.done", **counts)
    return counts


@app.task(name="cleanup_user_files")
async def cleanup_user_files(user_id: int) -> None:
    """After offboarding. Runs under the user's librarian lock, so never while their librarian writes."""
    removed = maintenance.remove_user_files(user_id)
    log.info("offboarding.files_removed", user_id=user_id, paths=removed)


async def defer_cleanup_after_offboarding(user_id: int, project_ids: list[int]) -> None:
    await defer_once(cleanup_user_files, f"user-cleanup:{user_id}", lock=f"librarian:{user_id}", user_id=user_id)
    for pid in project_ids:
        await defer_once(cleanup_project_cache, f"cache-cleanup:{pid}", lock=project_lock(pid), project_id=pid)


@app.periodic(cron="40 4 * * 0")
@app.task(name="repo_cache_sweep", queueing_lock="repo_cache_sweep")
async def repo_cache_sweep(timestamp: int) -> None:
    """Weekly: remove cache dirs that have no project row (design 01 §H9)."""
    async with get_sessionmaker()() as db:
        removed = await sweep_orphan_caches(db)
    if removed:
        log.info("repo_cache.swept", projects=removed)


@app.task(name="build_episode", retry=2)
async def build_episode_task(session_id: str) -> None:
    structlog.contextvars.bind_contextvars(task="build_episode", session_id=session_id)
    async with get_sessionmaker()() as db:
        await build_episode(db, uuid.UUID(session_id))
        await db.commit()


@app.periodic(cron="*/30 * * * *")
@app.task(name="episode_sweep", queueing_lock="episode_sweep")
async def episode_sweep(timestamp: int) -> None:
    async with get_sessionmaker()() as db:
        ids = await sessions_needing_episodes(db)
    for sid in ids:
        await defer_once(build_episode_task, f"episode:{sid}", session_id=str(sid))
    log.info("episode_sweep.done", deferred=len(ids))


@app.task(name="librarian_user")
async def librarian_user_task(user_id: int) -> None:
    structlog.contextvars.bind_contextvars(task="librarian", user_id=user_id)
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            return
        tz = user_tz(user)  # read before a rollback expires the instance
        try:
            await update_user_wiki(db, user)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            db.add(
                LibrarianRun(
                    user_id=user_id,
                    finished_at=datetime.now(UTC),
                    local_date=datetime.now(tz).date(),
                    status=LibrarianRunStatus.failed,
                    error=str(exc)[:2000],
                )
            )
            await db.commit()
            raise


def _now() -> datetime:  # patched in tests
    return datetime.now(UTC)


@app.periodic(cron="5 * * * *")
@app.task(name="librarian_tick", queueing_lock="librarian_tick")
async def librarian_tick(timestamp: int) -> None:
    """Hourly: start each user's nightly librarian once their local time reaches 02:00 (design 01 §H8)."""
    async with get_sessionmaker()() as db:
        user_ids = await users_due(db, _now())
    for uid in user_ids:
        await defer_once(librarian_user_task, f"librarian:{uid}", user_id=uid)
    if user_ids:
        log.info("librarian_tick.deferred", users=len(user_ids))


@app.periodic(cron="30 3 * * *")
@app.task(name="wiki_reconcile", queueing_lock="wiki_reconcile", lock="wiki_reconcile")
async def wiki_reconcile_task(timestamp: int) -> None:
    await reconcile_wiki()


@app.periodic(cron="*/10 * * * *")
@app.task(name="embed_missing", queueing_lock="embed_missing")
async def embed_missing_task(timestamp: int) -> None:
    async with get_sessionmaker()() as db:
        while await embed_missing(db):
            await db.commit()
