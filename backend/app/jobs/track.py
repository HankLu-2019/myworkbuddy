"""After the push and after the work (design 04 §6 "Tracking", §7 "Cleanup and limits").

- `track_pushed()` (hourly): asks GitLab or Gerrit about each pushed job's review request. Merged → `merged` (work
  item done), closed or abandoned → `closed` (work item back to approved), each with an inbox item.
- `cleanup()` (hourly): a job left in review gets a notice a day before `MWB_JOB_REVIEW_DAYS` and is discarded
  (workspace and key removed) when they're up; artifacts of finished jobs go after `MWB_ARTIFACT_RETENTION_DAYS`;
  keys still stored for jobs that ended are removed again (their removal failed earlier).

Both read in a short transaction, call out with none open (R1), and write each outcome in its own short
transaction that re-checks the status it read (a transition from the expected status, or nothing).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, select

from app.config import get_settings
from app.connectors.base import Connection, ConnectorError, call_json
from app.db import get_sessionmaker
from app.enums import JobStatus
from app.inbox.service import add_item
from app.integrations.service import get_credentials
from app.jobs import artifacts, ci, keys
from app.jobs.push import _gitlab_project, change_id
from app.jobs.state import ACTIVE, TERMINAL, transition_in
from app.knowledge.ingest.repos import project_integration
from app.logs import log
from app.models import Artifact, JiraIssue, Job, JobSecret, Project, WorkItem

S = JobStatus


async def _key_of(db, job: Job) -> str:
    item = await db.get(WorkItem, job.work_item_id)
    issue = await db.get(JiraIssue, item.jira_issue_id) if item else None
    return issue.key if issue else "A ticket"


async def _review_state(job: Job, conn: Connection, project: Project) -> str | None:
    """'merged', 'closed' or None (still open) for the job's merge request or Gerrit change."""
    push = job.push or {}
    if project.host == "gerrit":
        number = ci.change_number(push)
        q = f"/a/changes/{quote(str(number))}" if number else None
        if q:
            change = await call_json(conn, "GET", q)
        else:
            found = await call_json(conn, "GET", "/a/changes/", params={"q": f"change:{change_id(job.id)}"}) or []
            change = found[0] if found else None
        status = (change or {}).get("status")
        return {"MERGED": "merged", "ABANDONED": "closed"}.get(status or "")
    iid = push.get("mr_iid")
    if not iid:
        return None
    pid = _gitlab_project(project, conn.integration)
    mr = await call_json(conn, "GET", f"/api/v4/projects/{pid}/merge_requests/{iid}")
    return {"merged": "merged", "closed": "closed"}.get((mr or {}).get("state", ""))


async def track_pushed() -> dict[str, int]:
    counts = {"checked": 0, "merged": 0, "closed": 0, "pipelines_failed": 0, "errors": 0}
    async with get_sessionmaker()() as db:
        jobs = list(await db.scalars(select(Job).where(Job.status == S.pushed).order_by(Job.updated_at)))
        targets: list[tuple[Job, Project, Connection]] = []
        for job in jobs:
            project = await db.get(Project, job.project_id) if job.project_id else None
            integration = await project_integration(db, project) if project else None
            if project is None or integration is None or not integration.enabled:
                continue
            try:
                creds = await get_credentials(db, job.owner_id, integration)
            except Exception:  # noqa: BLE001 - unreadable or missing: can't ask; try next hour
                counts["errors"] += 1
                continue
            db.expunge(integration)
            targets.append((job, project, Connection(job.owner_id, integration, creds)))
        await db.commit()
    for job, project, conn in targets:
        counts["checked"] += 1
        pipeline = None
        try:
            outcome = await _review_state(job, conn, project)  # network: nothing open
            if outcome is None:
                pipeline = await ci.pipeline_state(job, conn, project)
        except ConnectorError as exc:
            counts["errors"] += 1
            log.warning("jobs_track.failed", job_id=str(job.id), code=exc.code)
            continue
        finally:
            await conn.close()
        if outcome is None:
            if pipeline is not None and await _save_pipeline(job, pipeline):
                counts["pipelines_failed"] += 1
            continue
        new = S.merged if outcome == "merged" else S.closed
        async with get_sessionmaker()() as db:
            moved = await transition_in(
                db, job.id, S.pushed, new, detail={"note": f"The review request was {outcome}."}
            )
            if moved is not None:
                key = await _key_of(db, job)
                link = (job.push or {}).get("mr_url") or (job.push or {}).get("change_url")
                title = f"{key} merged" if new == S.merged else f"{key}: the review request was closed"
                body = "The ticket is done." if new == S.merged else "The ticket is back in Approved; start a new job."
                add_item(db, job.owner_id, kind="job", severity="info" if new == S.merged else "warning", title=title,
                         body_md=body + (f" [Review request]({link})" if link else ""),
                         link=f"/work/{job.work_item_id}/job")  # fmt: skip
                counts[outcome] += 1
            await db.commit()
    log.info("jobs_track.done", **counts)
    return counts


async def _save_pipeline(job: Job, pipeline: dict[str, Any]) -> bool:
    """Store the pushed commit's pipeline (08 §F1); True when it newly failed (one inbox item per sha)."""
    async with get_sessionmaker()() as db:
        fresh = await db.scalar(select(Job).where(Job.id == job.id).with_for_update())
        push = dict(fresh.push or {}) if fresh is not None else {}
        if fresh is None or fresh.status != S.pushed or push.get("pushed_sha") != pipeline["sha"]:
            await db.commit()
            return False  # moved on meanwhile
        before = push.get("pipeline") or {}
        newly_failed = pipeline["status"] == "failed" and before.get("notified_sha") != pipeline["sha"]
        stored = {**pipeline, "checked_at": datetime.now(UTC).isoformat(),
                  "notified_sha": pipeline["sha"] if newly_failed else before.get("notified_sha")}  # fmt: skip
        if {k: v for k, v in stored.items() if k != "checked_at"} != {
            k: v for k, v in before.items() if k != "checked_at"
        }:
            fresh.push = {**push, "pipeline": stored}
        if newly_failed:
            key = await _key_of(db, fresh)
            names = ", ".join(f.get("name") or "CI" for f in pipeline.get("failed") or []) or "CI"
            add_item(db, fresh.owner_id, kind="job", severity="warning",
                     title=f"{key}: the pipeline failed ({names})"[:200],
                     body_md="Open the job to fix it with **Fix CI failure**.",
                     link=f"/work/{fresh.work_item_id}/job")  # fmt: skip
        await db.commit()
        return newly_failed


async def cleanup(now: datetime | None = None) -> dict[str, int]:
    from app.jobs.pipeline import cleanup as remove_workspace_and_key

    s = get_settings()
    now = now or datetime.now(UTC)
    counts = {"reminded": 0, "discarded": 0, "artifacts": 0, "keys": 0}
    review_until = now - timedelta(days=s.job_review_days)
    remind_after = now - timedelta(days=max(s.job_review_days - 1, 0))
    async with get_sessionmaker()() as db:
        waiting = list(
            await db.scalars(select(Job).where(Job.status == S.awaiting_review, Job.updated_at < remind_after))
        )
        await db.commit()
    for job in waiting:
        async with get_sessionmaker()() as db:
            if job.updated_at < review_until:
                moved = await transition_in(
                    db, job.id, S.awaiting_review, S.discarded, error_code="review_expired",
                    error=f"Discarded after {s.job_review_days} days without a review.",
                    detail={"note": f"Discarded after {s.job_review_days} days without a review."},
                )  # fmt: skip
                if moved is not None:
                    key = await _key_of(db, job)
                    add_item(db, job.owner_id, kind="job", severity="warning",
                             title=f"Job for {key} discarded after {s.job_review_days} days",
                             body_md="Its changes weren't reviewed in time; the ticket is back in Approved.",
                             link=f"/work/{job.work_item_id}/job")  # fmt: skip
                    counts["discarded"] += 1
                await db.commit()
                if moved is not None:
                    await remove_workspace_and_key(job.id)  # network: committed first (R1)
                continue
            fresh = await db.scalar(select(Job).where(Job.id == job.id).with_for_update())
            if fresh is None or fresh.status != S.awaiting_review or (fresh.push or {}).get("expiry_notice"):
                await db.commit()
                continue
            fresh.push = {**(fresh.push or {}), "expiry_notice": now.isoformat()}
            key = await _key_of(db, fresh)
            add_item(db, fresh.owner_id, kind="job", severity="warning",
                     title=f"Job for {key} waits for your review",
                     body_md="It's discarded tomorrow if it isn't reviewed: push it, request changes or discard it.",
                     link=f"/work/{fresh.work_item_id}/job")  # fmt: skip
            await db.commit()
            counts["reminded"] += 1
    counts["artifacts"] = await _prune_artifacts(now - timedelta(days=s.artifact_retention_days))
    counts["keys"] = await _stale_keys()
    log.info("jobs_cleanup.done", **counts)
    return counts


async def _prune_artifacts(before: datetime) -> int:
    """Artifacts of jobs that are over (not active, not pushed: a pushed job may get another round) and old."""
    keep = [str(st) for st in (*ACTIVE, S.pushed)]
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(Artifact)
                .join(Job, Job.id == Artifact.job_id)
                .where(Artifact.created_at < before, Job.status.not_in(keep))
                .limit(5000)
            )
        )
        for row in rows:
            artifacts.path_of(row).unlink(missing_ok=True)
        if rows:
            await db.execute(delete(Artifact).where(Artifact.id.in_([r.id for r in rows])))
        await db.commit()
    for owner_job in {artifacts.root() / f"{r.owner_id}" / f"{r.job_id}" for r in rows}:
        try:
            owner_job.rmdir()  # only when empty
        except OSError:
            pass
    return len(rows)


async def _stale_keys() -> int:
    """Keys still stored for jobs that ended or were pushed: their removal failed earlier; try again."""
    over = [str(st) for st in TERMINAL] + [str(S.pushed)]
    async with get_sessionmaker()() as db:
        ids: list[uuid.UUID] = list(
            await db.scalars(select(JobSecret.job_id).join(Job, Job.id == JobSecret.job_id).where(Job.status.in_(over)))
        )
        await db.commit()
    removed = 0
    for job_id in ids:
        try:
            await keys.remove(job_id)
            removed += 1
        except Exception as exc:  # noqa: BLE001 - the key expires anyway; next hour tries again
            log.warning("jobs_cleanup.key_failed", job_id=str(job_id), error=str(exc)[:200])
    return removed
