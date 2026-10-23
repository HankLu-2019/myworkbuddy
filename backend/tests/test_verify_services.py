# ruff: noqa: F811 - pytest fixtures imported from the job tests
"""Verification with containers, opt-in per repository (design 08 §F10; plan F10). Sysbox itself is manual."""

import uuid

from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.jobs import pipeline
from app.models import AuditLog, Project
from app.plans.schema import PlanDoc
from app.plans.validation import validate_plan
from tests.test_job_pipeline import approved_ticket, forge_url, job_row, plan_doc, runner, start  # noqa: F401 - fixtures

KEY = "PAY-12"


def _check(services_enabled: bool):
    sandbox = plan_doc()["sandbox"] | {"services": True}
    doc = PlanDoc.model_validate(plan_doc(sandbox=sandbox))
    return validate_plan(doc, jira_key=KEY, existing_paths=None, visible_refs=[], optional_hosts=[],
                         services_enabled=services_enabled)[1]  # fmt: skip


def test_services_on_a_repository_that_isnt_enabled_is_a_validation_error() -> None:
    off = _check(False)
    assert not off.ok and [i.field for i in off.issues if i.level == "error"] == ["sandbox.services"]
    on = _check(True)
    assert on.ok and "services" in on.needs_ack
    plain = validate_plan(PlanDoc.model_validate(plan_doc()), jira_key=KEY, existing_paths=None, visible_refs=[],
                          optional_hosts=[])[1]  # fmt: skip
    assert "services" not in plain.needs_ack and all(i.field != "sandbox.services" for i in plain.issues)


async def test_only_admins_enable_it_and_the_job_uses_the_services_preset(
    make_user, client_for, forge_url, runner
) -> None:
    doc = plan_doc(sandbox=plan_doc()["sandbox"] | {"services": True})
    t = await approved_ticket(make_user, forge_url, doc=doc)
    pid = t["project"].id
    alice = await client_for("alice", create=False)
    assert (await alice.put(f"/api/projects/{pid}/verify-services", json={"enabled": True})).status_code == 403
    # Approved while enabled, then turned off: the job refuses to start.
    r = await alice.post(f"/api/work-items/{t['item'].id}/jobs", json={"plan_revision_id": str(t["rev"].id)})
    assert r.status_code == 409 and "no longer allows" in r.json()["detail"]
    root = await make_user("root", role="admin")
    admin = await client_for("root", create=False)
    r = await admin.put(f"/api/projects/{pid}/verify-services", json={"enabled": True})
    assert r.status_code == 200 and r.json()["verify_services"] is True
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(AuditLog).where(AuditLog.action == "project.verify_services"))
    assert row is not None and row.user_id == root.id
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert runner.presets == ["agent", "verify-services"]
    async with get_sessionmaker()() as db:
        await db.execute(update(Project).where(Project.id == pid).values(verify_services=False))
        await db.commit()


async def test_the_system_page_shows_whether_sysbox_is_installed(make_user, client_for, runner) -> None:
    await make_user("root", role="admin")
    admin = await client_for("root", create=False)
    sandbox = (await admin.get("/api/admin/system")).json()["sandbox"]
    assert sandbox["ok"] and sandbox["services"] == "Runtime sysbox-runc isn't installed"
    runner.services = "ok"
    assert (await admin.get("/api/admin/system")).json()["sandbox"]["services"] == "ok"


async def test_turning_it_off_applies_to_jobs_already_running(make_user, client_for, forge_url, runner) -> None:
    """Checked at every verify, not only at job start: a later round can't keep using containers."""
    doc = plan_doc(sandbox=plan_doc()["sandbox"] | {"services": True})
    t = await approved_ticket(make_user, forge_url, doc=doc)
    async with get_sessionmaker()() as db:
        await db.execute(update(Project).where(Project.id == t["project"].id).values(verify_services=True))
        await db.commit()
    job_id = await start(client_for, t)
    async with get_sessionmaker()() as db:  # the admin changes their mind while the job runs
        await db.execute(update(Project).where(Project.id == t["project"].id).values(verify_services=False))
        await db.commit()
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    job = await job_row(job_id)
    assert job.error_code == "services_disabled" and "turned off containers" in job.error
    assert "verify-services" not in runner.presets
