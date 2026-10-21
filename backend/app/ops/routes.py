"""Readiness and the admin System page (design 01 §H14, 07-ux §4 Admin → System)."""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.audit.service import audit
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import DB, AdminAuth, client_ip
from app.errors import AppError
from app.knowledge.indexer import MAX_EMBED_ATTEMPTS
from app.models import Chunk
from app.ops import backup, health

router = APIRouter(prefix="/api", tags=["ops"])

STATE_KEYS = (
    backup.STATE_KEY,
    backup.DRILL_KEY,
    health.HEARTBEAT_KEY,
    "wiki_reconcile",
    "disk_guard",
)


@router.get("/ready")
async def ready() -> JSONResponse:
    """200 when the database, gateway and worker are fine; 503 naming what isn't. Unauthenticated, like
    /api/health, and reveals only which dependency is down."""
    checks = await health.readiness()
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


async def _jobs(db: DB) -> dict[str, Any]:
    by_queue = [
        {"queue": q, "status": s, "count": n}
        for q, s, n in await db.execute(
            text(
                "SELECT queue_name, status::text, count(*) FROM procrastinate_jobs "
                "WHERE status IN ('todo', 'doing') GROUP BY 1, 2 ORDER BY 1, 2"
            )
        )
    ]
    since = datetime.now(UTC) - timedelta(hours=24)
    failed = [
        {"id": i, "queue": q, "task": t, "attempts": a, "failed_at": at.isoformat(), "args": args}
        for i, q, t, a, at, args in await db.execute(
            text(
                "SELECT j.id, j.queue_name, j.task_name, j.attempts, max(e.at), j.args "
                "FROM procrastinate_jobs j JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'failed' "
                "WHERE j.status = 'failed' AND e.at >= :since "
                "GROUP BY j.id ORDER BY max(e.at) DESC LIMIT 50"
            ),
            {"since": since},
        )
    ]
    periodic = [
        {"task": t, "last_at": datetime.fromtimestamp(ts, UTC).isoformat()}
        for t, ts in await db.execute(
            text("SELECT task_name, max(defer_timestamp) FROM procrastinate_periodic_defers GROUP BY 1 ORDER BY 1")
        )
    ]
    return {"queues": by_queue, "failed_24h": failed, "periodic": periodic}


def _disk() -> dict[str, Any]:
    s = get_settings()
    try:
        usage = shutil.disk_usage(s.data_dir)
    except OSError:
        return {"path": str(s.data_dir), "error": "unavailable"}
    return {
        "path": str(s.data_dir),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "free_ratio": round(usage.free / usage.total, 3) if usage.total else None,
    }


async def _spend() -> dict[str, Any]:
    from app.llm import keys
    from app.models import User

    async with get_sessionmaker()() as db:
        ids = list(await db.scalars(select(User.id).where(User.is_active)))
    infos = await asyncio.gather(*(keys.key_info(i) for i in ids), return_exceptions=True)
    known = [i for i in infos if isinstance(i, dict)]
    return {
        "month_to_date_usd": round(sum(i["spend_usd"] for i in known), 2),
        "users_counted": len(known),
        "users_unknown": len(ids) - len(known),
    }


def _alerts(state: dict[str, dict[str, Any]], checks: dict[str, str], stuck: int, disk: dict[str, Any]) -> list[str]:
    alerts = [v for v in checks.values() if v != "ok"]
    if problem := backup.status_problem(state.get(backup.STATE_KEY)):
        alerts.append(problem)
    guard = state.get("disk_guard")
    if guard and not guard.get("ok", True):
        alerts.append(guard.get("problem") or "Repository disk guard tripped")
    if disk.get("free_ratio") is not None and disk["free_ratio"] < 0.1:
        alerts.append(f"Only {disk['free_ratio']:.0%} of the data disk is free")
    rec = state.get("wiki_reconcile") or {}
    if rec.get("missing"):
        alerts.append(f"{len(rec['missing'])} wiki page(s) are missing from git")
    if stuck:
        alerts.append(f"{stuck} chunk(s) failed embedding {MAX_EMBED_ATTEMPTS} times")
    return alerts


@router.get("/admin/system")
async def system(auth: AdminAuth, db: DB) -> dict[str, Any]:
    """Everything the System page shows (design 01 §H14). Each block degrades on its own."""
    states = await health.get_states(*STATE_KEYS)
    jobs = await _jobs(db)
    stuck = int(
        await db.scalar(
            select(func.count()).where(Chunk.embedding.is_(None), Chunk.embed_attempts >= MAX_EMBED_ATTEMPTS)
        )
        or 0
    )
    stuck_sample = [
        {"chunk_id": i, "document_id": d, "error": (e or "")[:200]}
        for i, d, e in await db.execute(
            select(Chunk.id, Chunk.document_id, Chunk.embed_error)
            .where(Chunk.embedding.is_(None), Chunk.embed_attempts >= MAX_EMBED_ATTEMPTS)
            .order_by(Chunk.id)
            .limit(20)
        )
    ]
    await db.commit()  # rule R1: the gateway checks below are network calls
    checks, spend = await asyncio.gather(health.readiness(), _spend())
    disk = _disk()
    hb = states.get(health.HEARTBEAT_KEY)
    age = health.heartbeat_age(hb)
    return {
        "checks": checks,
        "alerts": _alerts(states, checks, stuck, disk),
        "worker": {"heartbeat": hb, "age_s": int(age.total_seconds()) if age else None},
        "jobs": jobs,
        "backup": states.get(backup.STATE_KEY) or {"status": "not_configured"},
        "backup_drill": states.get(backup.DRILL_KEY),
        "disk": disk,
        "disk_guard": states.get("disk_guard"),
        "wiki_reconcile": states.get("wiki_reconcile"),
        "embeddings": {"failed": stuck, "sample": stuck_sample},
        "llm_spend": spend,
    }


@router.post("/admin/system/jobs/{job_id}/retry")
async def retry_job(job_id: int, auth: AdminAuth, db: DB, request: Request) -> dict[str, Any]:
    """Re-queue a failed job now (same task, args and lock)."""
    from app.tasks.app import app as tasks_app

    status_ = await db.scalar(text("SELECT status::text FROM procrastinate_jobs WHERE id = :id"), {"id": job_id})
    if status_ is None:
        raise AppError(404, "not_found", "Job not found")
    if status_ != "failed":
        raise AppError(409, "conflict", f"Only failed jobs can be retried (this one is {status_})")
    await audit(db, "job.retry", user_id=auth.user.id, target_type="job", target_id=str(job_id), ip=client_ip(request))
    await db.commit()
    await tasks_app.job_manager.retry_job_by_id_async(job_id, retry_at=datetime.now(UTC))
    return {"retried": job_id}
