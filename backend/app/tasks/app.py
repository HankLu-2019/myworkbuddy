import uuid

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.knowledge.episodes import build_episode, sessions_needing_episodes
from app.knowledge.indexer import embed_missing
from app.knowledge.ingest.repos import ingest_project
from app.knowledge.librarian import update_user_wiki
from app.logs import log, setup_logging
from app.models import User

setup_logging(get_settings().log_level)

app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=get_settings().database_url))


async def defer_once(task: procrastinate.tasks.Task, lock: str, **kwargs: object) -> bool:
    """Queue the task unless an identical one is already waiting. Returns whether it was queued."""
    try:
        await task.configure(queueing_lock=lock).defer_async(**kwargs)
        return True
    except AlreadyEnqueued:
        return False


@app.task(name="ingest_project", retry=False)
async def ingest_project_task(project_id: int, force: bool = False) -> None:
    structlog.contextvars.bind_contextvars(task="ingest_project", project_id=project_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, project_id, force=force)


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
        if user and user.is_active:
            await update_user_wiki(db, user)
            await db.commit()


@app.periodic(cron="0 2 * * *")
@app.task(name="librarian_nightly", queueing_lock="librarian_nightly")
async def librarian_nightly(timestamp: int) -> None:
    async with get_sessionmaker()() as db:
        user_ids = list(await db.scalars(select(User.id).where(User.is_active)))
    for uid in user_ids:
        await defer_once(librarian_user_task, f"librarian:{uid}", user_id=uid)


@app.periodic(cron="*/10 * * * *")
@app.task(name="embed_missing", queueing_lock="embed_missing")
async def embed_missing_task(timestamp: int) -> None:
    async with get_sessionmaker()() as db:
        while await embed_missing(db):
            await db.commit()
