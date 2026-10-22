# ruff: noqa: F811 - pytest fixtures imported from the job tests
"""CI failure → Jira ticket → work item (design 08 §F6; plan F6)."""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.agents.seeds import sync_system_agents
from app.db import get_sessionmaker
from app.enums import ApprovalSubject
from app.models import Agent, Approval, FindingTicket, JiraIssue, Plan, Run, WorkItem
from app.plans import tickets
from app.plans.schema import canonical_json
from tests.test_job_pipeline import approved_ticket, forge_url  # noqa: F401 - fixtures
from tests.test_job_push import Crash, forge, state  # noqa: F401 - fixtures

FP = "gitlab:team/payments:pipeline:55:test:unit"
FINDING = {
    "fingerprint": FP, "severity": "alert", "title": "payments main is broken: test:unit fails",
    "detail": "`test_retries` fails with `AssertionError: retries == 0`.", "verified": True,
    "links": ["https://git.example.com/team/payments/-/jobs/551"],
}  # fmt: skip


async def ci_run(user_id: int, agent_slug: str = "ci-failures") -> uuid.UUID:
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        agent = await db.scalar(select(Agent).where(Agent.slug == agent_slug, Agent.owner_id.is_(None)))
        run = Run(owner_id=user_id, agent_id=agent.id, trigger="manual", status="succeeded",
                  config={"agent_slug": agent_slug, "name": "CI watch"}, scheduled_for=datetime.now(UTC),
                  report={"schema_version": 1, "status": "alert", "headline": "1 failure", "findings": [FINDING],
                          "summary_md": ""}, fingerprints=[FP])  # fmt: skip
        db.add(run)
        await db.commit()
        return run.id


async def setup(make_user, forge_url) -> tuple[dict, uuid.UUID]:
    t = await approved_ticket(make_user, forge_url, jira_url=forge_url)
    return t, await ci_run(t["user"].id)


async def test_a_finding_becomes_an_issue_a_work_item_and_a_plan(
    make_user, client_for, forge_url, forge, task_queue
) -> None:
    t, run_id = await setup(make_user, forge_url)
    c = await client_for("alice", create=False)
    r = await c.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": FP})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["existing"] is None and d["projects"][0] == {"key": "PAY", "name": "Payments"}
    assert d["project_key"] == "PAY" and d["repository_id"] == t["project"].id and d["issue_type"] == "Bug"
    assert d["summary"] == FINDING["title"] and FINDING["detail"] in d["description"]
    assert (
        f"/runs/{run_id}" in d["description"] and "https://git.example.com/team/payments/-/jobs/551" in d["description"]
    )
    body = {k: d[k] for k in ("fingerprint", "integration_id", "project_key", "issue_type", "summary", "labels")}
    body |= {"description": d["description"] + "\n\nSeen twice this week.", "autoplan": True,
             "repository_id": t["project"].id}  # fmt: skip
    r = await c.post(f"/api/runs/{run_id}/tickets", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["jira_key"] == "PAY-1001" and out["created"] and out["plan_started"]
    s = await state(forge)
    [issue] = s["issues"]
    label = tickets.fp_label(t["user"].id, FP)
    assert issue["fields"]["labels"] == sorted(["mwb-ci", label])
    assert issue["fields"]["description"].endswith("Seen twice this week.")  # exactly the approved text
    async with get_sessionmaker()() as db:
        approval = await db.scalar(select(Approval).where(Approval.subject_type == ApprovalSubject.jira_issue))
        item = await db.get(WorkItem, uuid.UUID(out["work_item_id"]))
        key = await db.scalar(select(JiraIssue.key).where(JiraIssue.id == item.jira_issue_id))
        plan = await db.scalar(select(Plan).where(Plan.work_item_id == item.id))
        row = await db.scalar(select(FindingTicket))
    data = tickets.payload(tickets.TicketIn(**body), t["user"].id)
    assert approval.payload_hash == hashlib.sha256(canonical_json(data).encode()).hexdigest()
    assert key == "PAY-1001" and item.project_id == t["project"].id and plan is not None
    assert row.jira_key == "PAY-1001" and row.work_item_id == item.id
    assert [j for j in task_queue.jobs.values() if j["task_name"] == "draft_plan"]
    # The same finding now opens the issue instead of making another.
    again = await c.post(f"/api/runs/{run_id}/tickets", json=body)
    assert again.status_code == 409 and again.json()["jira_key"] == "PAY-1001"
    d2 = (await c.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": FP})).json()
    assert d2["existing"]["jira_key"] == "PAY-1001"


async def test_a_crash_after_jira_answered_doesnt_make_a_second_issue(
    make_user, client_for, forge_url, forge, monkeypatch
) -> None:
    t, run_id = await setup(make_user, forge_url)
    c = await client_for("alice", create=False)
    d = (await c.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": FP})).json()
    body = {k: d[k] for k in ("fingerprint", "integration_id", "project_key", "issue_type", "summary", "description",
                              "labels", "autoplan")}  # fmt: skip
    real = tickets.jira.create_issue

    async def create_then_die(conn, fields):
        await real(conn, fields)
        raise Crash

    monkeypatch.setattr(tickets.jira, "create_issue", create_then_die)
    with pytest.raises(Crash):  # the worker dies between Jira's answer and our write
        async with get_sessionmaker()() as db:
            await tickets.create(db, run_id, tickets.TicketIn(**body), t["user"], None)
    monkeypatch.setattr(tickets.jira, "create_issue", real)
    r = await c.post(f"/api/runs/{run_id}/tickets", json=body)
    assert r.status_code == 201 and r.json()["created"] is False and r.json()["jira_key"] == "PAY-1001"
    assert len((await state(forge))["issues"]) == 1


async def test_ticket_requests_are_checked(make_user, client_for, forge_url, forge) -> None:
    t, run_id = await setup(make_user, forge_url)
    c = await client_for("alice", create=False)
    base = {"fingerprint": FP, "integration_id": 0, "project_key": "PAY", "issue_type": "Bug", "summary": "x",
            "description": "y"}  # fmt: skip
    d = (await c.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": FP})).json()
    assert (await c.post(f"/api/runs/{run_id}/tickets", json=base)).status_code == 409  # not one of the user's Jiras
    ok = {**base, "integration_id": d["integration_id"]}
    r = await c.post(f"/api/runs/{run_id}/tickets", json={**ok, "autoplan": True})
    assert r.status_code == 422 and r.json()["field"] == "repository_id"
    assert (await c.post(f"/api/runs/{run_id}/tickets", json={**ok, "project_key": "not a key"})).status_code == 422
    missing = await c.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": "other"})
    assert missing.status_code == 404
    bob = await client_for("bob")
    assert (await bob.get(f"/api/runs/{run_id}/ticket-draft", params={"fingerprint": FP})).status_code == 404
    k8s = await ci_run(t["user"].id, "k8s-health")
    r = await c.get(f"/api/runs/{k8s}/ticket-draft", params={"fingerprint": FP})
    assert r.status_code == 409 and "CI failure runs only" in r.json()["detail"]
    assert (await state(forge))["issues"] == []
