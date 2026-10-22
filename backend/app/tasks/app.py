import uuid
from datetime import UTC, datetime
from decimal import Decimal

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import func, select, update

from app import maintenance
from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import LibrarianRunStatus
from app.knowledge.episodes import build_episode, sessions_needing_episodes
from app.knowledge.indexer import embed_missing
from app.knowledge.ingest.repos import ingest_project, remove_cache, sweep_orphan_caches
from app.knowledge.librarian import update_user_wiki, user_tz, users_due
from app.knowledge.reconcile import reconcile_wiki
from app.llm import keys
from app.logs import log, setup_logging
from app.models import LibrarianRun, Message, User

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
    await defer_once(delete_gateway_key, f"gateway-key-delete:{user_id}", user_id=user_id)
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


class NoRetryOnBudget(procrastinate.RetryStrategy):
    """Retry like RetryStrategy, except when the user's AI budget is exhausted (design 01 §H12): retrying
    can't succeed until the budget resets or an admin raises it."""

    def get_retry_decision(self, *, exception: BaseException, job):  # type: ignore[override]
        if getattr(exception, "code", None) == "budget_exceeded":
            log.warning("task.budget_exceeded", task=job.task_name)
            return None
        return super().get_retry_decision(exception=exception, job=job)


SETTLE_ATTEMPTS = 5


@app.task(name="settle_costs")
async def settle_costs(message_id: int, call_ids: list[str], attempt: int = 1) -> None:
    """Add the cost of streamed LLM calls to a message once the gateway's spend log has them (LiteLLM writes
    it in batches). Calls not logged yet are retried a minute later, up to SETTLE_ATTEMPTS times."""
    found = Decimal(0)
    missing = []
    for call_id in call_ids:
        cost = await keys.spend_for_call(call_id)
        if cost is None:
            missing.append(call_id)
        else:
            found += cost
    if found:
        async with get_sessionmaker()() as db:
            await db.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(cost_usd=func.coalesce(Message.cost_usd, 0) + found)
            )
            await db.commit()
    if missing and attempt < SETTLE_ATTEMPTS:
        await defer_settle_costs(message_id, missing, delay=60, attempt=attempt + 1)
    elif missing:
        log.warning("llm.costs_unsettled", message_id=message_id, calls=len(missing))


async def defer_settle_costs(message_id: int, call_ids: list[str], *, delay: int = 90, attempt: int = 1) -> None:
    await settle_costs.configure(schedule_in={"seconds": delay}, lock=f"message-cost:{message_id}").defer_async(
        message_id=message_id, call_ids=call_ids, attempt=attempt
    )


@app.task(name="delete_gateway_key", retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10))
async def delete_gateway_key(user_id: int) -> None:
    """After offboarding: delete the user's LiteLLM key by alias (no secret needed, the row is gone)."""
    await keys.delete_key(user_id)
    log.info("llm.key_deleted", user_id=user_id)


@app.task(name="build_episode", retry=NoRetryOnBudget(max_attempts=2))
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


# ---------- schedules (design 02 §4) ----------


def schedule_lock(schedule_id: uuid.UUID | None, run_id: uuid.UUID) -> str:
    """Runs of one schedule never overlap; a run without a schedule (a test run) only locks itself."""
    return f"schedule:{schedule_id}" if schedule_id else f"run:{run_id}"


async def defer_run(run_id: uuid.UUID, schedule_id: uuid.UUID | None) -> bool:
    return await defer_once(
        run_schedule_task, f"run:{run_id}", lock=schedule_lock(schedule_id, run_id), run_id=str(run_id)
    )


@app.task(name="run_schedule", queue="llm", retry=False)
async def run_schedule_task(run_id: str) -> None:
    from app.schedules.service import execute_run

    structlog.contextvars.bind_contextvars(task="run_schedule", run_id=run_id)
    await execute_run(uuid.UUID(run_id))


@app.periodic(cron="* * * * *")
@app.task(name="schedule_tick", queueing_lock="schedule_tick")
async def schedule_tick(timestamp: int) -> None:
    from app.schedules.service import tick

    result = await tick()
    for run_id, schedule_id in result.created + result.requeue:
        await defer_run(run_id, schedule_id)
    if result.created or result.requeue or result.interrupted or result.disabled:
        log.info(
            "schedule_tick.done",
            created=len(result.created),
            requeued=len(result.requeue),
            interrupted=result.interrupted,
            disabled=result.disabled,
        )


@app.periodic(cron="* * * * *")
@app.task(name="send_emails", queueing_lock="send_emails", lock="send_emails")
async def send_emails(timestamp: int) -> None:
    """The email outbox (design 02 §5)."""
    from app.inbox.sender import send_pending

    await send_pending()


# ---------- Phase 2 system tasks (design 02 §9) ----------


@app.periodic(cron="0 1 * * *")
@app.task(name="projects_refresh", queueing_lock="projects_refresh", lock="projects_refresh")
async def projects_refresh(timestamp: int) -> dict[str, int]:
    """Nightly at 01:00 UTC: re-ingest ready repositories whose branch moved."""
    from app.knowledge.ingest.refresh import refresh_projects

    result = await refresh_projects()
    queued = sum([await defer_ingest(pid) for pid in result.moved])
    counts = {"checked": result.checked, "moved": len(result.moved), "queued": queued, "failed": len(result.failed)}
    log.info("projects_refresh.done", **counts)
    return counts


@app.periodic(cron="40 2 * * *")
@app.task(name="runs_retention", queueing_lock="runs_retention", lock="runs_retention")
async def runs_retention(timestamp: int) -> dict[str, int]:
    from app.schedules.retention import prune_runs

    counts = await prune_runs()
    log.info("runs_retention.done", **counts)
    return counts


@app.periodic(cron="10 3 * * *")
@app.task(name="agent_evals", queueing_lock="agent_evals", lock="agent_evals")
async def agent_evals(timestamp: int) -> dict[str, int] | None:
    """Nightly agent evals against the real model, when MWB_AGENT_EVALS_USER is set (not a CI gate)."""
    from app.agents.evals import STATE_KEY, run_evals, summary
    from app.ops.health import put_state

    username = get_settings().agent_evals_user
    if not username:
        return None
    s = summary(await run_evals(username))
    await put_state(STATE_KEY, s | {"at": _now().isoformat()})
    log.info("agent_evals.done", passed=s["passed"], total=s["total"], cost_usd=s["cost_usd"])
    return {"passed": s["passed"], "total": s["total"]}


# ---------- Phase 3: Jira intake (design 03 §7) ----------


@app.task(name="jira_intake", retry=False)
async def jira_intake(user_id: int) -> dict[str, int]:
    from app.plans.planner import autoplan
    from app.plans.work_items import run_intake

    structlog.contextvars.bind_contextvars(task="jira_intake", user_id=user_id)
    r = await run_intake(user_id)
    planned = await autoplan(user_id, r.created)
    counts = {"issues": r.issues, "created": len(r.created), "failed": len(r.failed), "autoplanned": len(planned)}
    log.info("jira_intake.done", **counts)
    return counts


@app.periodic(cron="*/30 * * * *")
@app.task(name="jira_intake_tick", queueing_lock="jira_intake_tick")
async def jira_intake_tick(timestamp: int) -> int:
    async with get_sessionmaker()() as db:
        ids = list(await db.scalars(select(User.id).where(User.is_active, User.jira_intake_enabled)))
    for uid in ids:
        await defer_once(jira_intake, f"intake:{uid}", user_id=uid)
    return len(ids)


@app.task(name="draft_plan", queue="llm", retry=False)
async def draft_plan_task(plan_id: str, feedback: str | None = None) -> None:
    """Draft or revise a plan (design 03 §5); locked per repository, because ingest shares its git cache."""
    from app.plans.planner import draft_plan

    structlog.contextvars.bind_contextvars(task="draft_plan", plan_id=plan_id)
    await draft_plan(uuid.UUID(plan_id), feedback)
