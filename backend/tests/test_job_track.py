# ruff: noqa: F811 - pytest fixtures imported from the job tests
"""Tracking pushed jobs and the cleanup table (design 04 §6 Tracking, §7; plan X7)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs import pipeline, track
from app.jobs.artifacts import path_of
from app.models import Artifact, InboxItem, Job, JobSecret, WorkItem
from tests.test_job_pipeline import (  # noqa: F401 - fixtures
    approved_ticket,
    forge_url,
    job_row,
    key_record,
    runner,
    start,
)
from tests.test_job_push import approve, forge, reviewed  # noqa: F401 - fixtures


async def pushed(make_user, client_for, forge_url, **kw) -> tuple[dict, str]:
    t, job_id = await reviewed(make_user, client_for, forge_url, **kw)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    return t, job_id


async def titles(user_id: int) -> list[str]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(InboxItem.title).where(InboxItem.owner_id == user_id)))


async def item_state(job: Job) -> str:
    async with get_sessionmaker()() as db:
        return (await db.get(WorkItem, job.work_item_id)).state


async def test_a_merged_merge_request_finishes_the_job_and_the_ticket(
    make_user, client_for, forge_url, forge, runner
) -> None:
    t, job_id = await pushed(make_user, client_for, forge_url)
    assert (await track.track_pushed())["merged"] == 0  # still open
    assert (await forge.post("/fake/merge/1")).status_code == 200
    counts = await track.track_pushed()
    assert counts["checked"] == 1 and counts["merged"] == 1
    job = await job_row(job_id)
    assert job.status == "merged" and await item_state(job) == "done"
    assert "PAY-12 merged" in await titles(t["user"].id)
    assert (await track.track_pushed())["checked"] == 0  # nothing pushed any more


async def test_a_closed_merge_request_puts_the_ticket_back_to_approved(
    make_user, client_for, forge_url, forge, runner
) -> None:
    t, job_id = await pushed(make_user, client_for, forge_url)
    await forge.post("/fake/merge/1", params={"state": "closed"})
    assert (await track.track_pushed())["closed"] == 1
    job = await job_row(job_id)
    assert job.status == "closed" and await item_state(job) == "approved"
    assert "PAY-12: the review request was closed" in await titles(t["user"].id)


async def test_a_merged_gerrit_change_is_tracked(make_user, client_for, forge_url, forge, runner) -> None:
    _t, job_id = await pushed(make_user, client_for, forge_url, host="gerrit", token="gerrit-pass")
    assert (await job_row(job_id)).push["change_number"] == 1
    await forge.post("/fake/gerrit/1", params={"status": "MERGED"})
    assert (await track.track_pushed())["merged"] == 1
    assert (await job_row(job_id)).status == "merged"


async def test_a_job_left_in_review_is_reminded_then_discarded(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    days = get_settings().job_review_days

    async def age(d: float) -> None:
        async with get_sessionmaker()() as db:
            await db.execute(
                update(Job).where(Job.id == uuid.UUID(job_id)).values(updated_at=datetime.now(UTC) - timedelta(days=d))
            )
            await db.commit()

    await age(days - 2)
    assert (await track.cleanup())["reminded"] == 0
    await age(days - 0.5)
    assert (await track.cleanup())["reminded"] == 1
    assert (await track.cleanup())["reminded"] == 0  # once
    assert "Job for PAY-12 waits for your review" in await titles(t["user"].id)
    assert job_id in runner.workspaces
    await age(days + 0.1)
    assert (await track.cleanup())["discarded"] == 1
    job = await job_row(job_id)
    assert job.status == "discarded" and job.error_code == "review_expired" and await item_state(job) == "approved"
    assert job_id not in runner.workspaces and key_record(job_id) is None
    async with get_sessionmaker()() as db:
        assert await db.get(JobSecret, job.id) is None
    assert f"Job for PAY-12 discarded after {days} days" in await titles(t["user"].id)


async def test_old_artifacts_of_finished_jobs_go_and_stale_keys_are_removed(
    make_user, client_for, forge_url, runner
) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    c = await client_for("alice", create=False)
    assert (await c.post(f"/api/jobs/{job_id}/discard")).status_code == 200
    async with get_sessionmaker()() as db:
        rows = list(await db.scalars(select(Artifact).where(Artifact.job_id == uuid.UUID(job_id))))
        old = datetime.now(UTC) - timedelta(days=get_settings().artifact_retention_days + 1)
        await db.execute(update(Artifact).where(Artifact.id == rows[0].id).values(created_at=old))
        # a key whose removal failed when the job ended
        db.add(JobSecret(job_id=uuid.UUID(job_id), ciphertext="x"))
        await db.commit()
    files = [path_of(r) for r in rows]
    assert all(f.is_file() for f in files)
    counts = await track.cleanup()
    assert counts["artifacts"] == 1 and counts["keys"] == 1
    assert not files[0].exists() and all(f.is_file() for f in files[1:])
    async with get_sessionmaker()() as db:
        assert await db.get(Artifact, rows[0].id) is None and await db.get(JobSecret, uuid.UUID(job_id)) is None


async def test_a_workspace_past_the_disk_limit_fails_the_job(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    runner.agent = ["disk_limit"]
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    job = await job_row(job_id)
    assert job.error_code == "workspace_too_large" and job.failed_stage == "running"
    assert job_id not in runner.workspaces


async def test_the_periodic_tasks_run_both() -> None:
    from app.tasks import app as tasks

    assert set(await tasks.jobs_track(timestamp=0)) == {"checked", "merged", "closed", "pipelines_failed", "errors"}
    assert set(await tasks.jobs_cleanup(timestamp=0)) == {"reminded", "discarded", "artifacts", "keys"}
