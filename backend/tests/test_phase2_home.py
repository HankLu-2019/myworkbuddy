"""Home and run-detail data for the Phase 2 pages (07-ux §4.1, §4.8, §5.10)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.agents.seeds import sync_system_agents
from app.db import get_sessionmaker
from app.inbox.service import add_item
from app.models import Agent, Run, Schedule, User


@pytest.fixture(autouse=True)
async def agents() -> None:
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        await db.commit()


async def _alice() -> User:
    async with get_sessionmaker()() as db:
        return await db.scalar(select(User).where(User.username == "alice"))


def _report(*fps: str, status: str = "alert") -> dict:
    return {
        "status": status, "headline": f"{len(fps)} problems", "summary_md": "",
        "findings": [{"fingerprint": f, "severity": "alert", "title": f"T {f}", "detail": "", "links": [],
                      "verified": True} for f in fps],
    }  # fmt: skip


async def _run(owner: User, agent_slug: str, *, schedule_id=None, report=None, status="succeeded", ago_h=1.0) -> Run:
    async with get_sessionmaker()() as db:
        agent = await db.scalar(select(Agent).where(Agent.slug == agent_slug))
        at = datetime.now(UTC) - timedelta(hours=ago_h)
        run = Run(
            owner_id=owner.id, agent_id=agent.id, schedule_id=schedule_id, trigger="schedule", status=status,
            scheduled_for=at, finished_at=at, report=report,
            fingerprints=[f["fingerprint"] for f in (report or {}).get("findings", [])],
            config={"name": "Brief", "agent_slug": agent_slug,
                    "schedule_id": str(schedule_id) if schedule_id else None},
        )  # fmt: skip
        db.add(run)
        await db.commit()
        await db.execute(update(Run).where(Run.id == run.id).values(created_at=at))
        await db.commit()
        return run


async def test_home_shows_alerts_paused_schedules_and_todays_brief(client_for) -> None:
    c = await client_for("alice")
    alice = await _alice()
    s = (
        await c.post(
            "/api/schedules",
            json={"name": "K8s health", "agent": "k8s-health", "cron": "0 8 * * *", "inputs": {"namespaces": ["prod"]}},
        )
    ).json()
    async with get_sessionmaker()() as db:
        add_item(db, alice.id, kind="alert", severity="alert", title="K8s health: 1 crash loop", link="/runs/x")
        add_item(db, alice.id, kind="run", severity="info", title="Not a card")
        await db.execute(
            update(Schedule).values(enabled=False, next_run_at=None, paused_reason="Paused after 3 failures: boom")
        )
        await db.commit()
    await _run(alice, "morning-brief", report=_report("jira:SPIN-1", status="warning"))
    await _run(alice, "morning-brief", report=_report(), ago_h=30)  # older than a day
    home = (await c.get("/api/home")).json()
    cards = {n["kind"]: n for n in home["needs_you"]}
    assert cards["alert"]["message"] == "K8s health: 1 crash loop" and cards["alert"]["href"] == "/runs/x"
    assert cards["schedule_paused"]["message"] == "Paused after 3 failures: boom"
    assert (
        cards["schedule_paused"]["href"] == f"/schedules/{s['id']}" and cards["schedule_paused"]["action"] == "Review"
    )
    assert [n["kind"] for n in home["needs_you"]].count("alert") == 1
    assert home["today"]["headline"] == "1 problems" and home["today"]["status"] == "warning"
    # Fixing it clears the card (07 §5.10): reading the alert, resuming the schedule.
    await c.post("/api/inbox/read-all")
    await c.patch(f"/api/schedules/{s['id']}", json={"enabled": True})
    assert (await c.get("/api/home")).json()["needs_you"] == []


async def test_activity_lists_running_runs(client_for) -> None:
    c = await client_for("alice")
    alice = await _alice()
    run = await _run(alice, "jira-digest", status="running")
    items = (await c.get("/api/activity")).json()["items"]
    assert items == [
        {"kind": "run", "id": str(run.id), "title": "Brief", "status": "running", "step": "Running checks",
         "since": items[0]["since"], "href": f"/runs/{run.id}"}
    ]  # fmt: skip


async def test_run_detail_shows_new_and_resolved_findings(client_for) -> None:
    c = await client_for("alice")
    alice = await _alice()
    sid = uuid.UUID(
        (
            await c.post(
                "/api/schedules",
                json={"name": "Digest", "agent": "jira-digest", "cron": "0 8 * * *", "inputs": {"jql": "x"}},
            )
        ).json()["id"]
    )
    first = await _run(alice, "jira-digest", schedule_id=sid, report=_report("jira:A-1", "jira:A-2"), ago_h=48)
    second = await _run(alice, "jira-digest", schedule_id=sid, report=_report("jira:A-2", "jira:A-3"), ago_h=24)
    d1 = (await c.get(f"/api/runs/{first.id}")).json()
    assert d1["new_fingerprints"] == ["jira:A-1", "jira:A-2"] and d1["resolved"] == [] and d1["previous_run_id"] is None
    d2 = (await c.get(f"/api/runs/{second.id}")).json()
    assert d2["new_fingerprints"] == ["jira:A-3"] and d2["previous_run_id"] == str(first.id)
    assert d2["resolved"] == [{"fingerprint": "jira:A-1", "title": "T jira:A-1", "severity": "alert"}]


async def test_the_fake_gateway_runs_a_morning_brief_with_a_verified_alert(client_for, api) -> None:
    """The path the end-to-end flow 07 §5.7 relies on: real executor, fake gateway, a feed as the only tool."""
    from app.llm.models import set_model_override
    from app.models import Integration
    from app.schedules.service import execute_run
    from tests.connector_fakes import fixture, ok

    url = "https://feeds.example.com/fake/feed.xml"
    async with get_sessionmaker()() as db:
        db.add(Integration(kind="feed", name="Status", base_url="https://feeds.example.com", settings={"urls": [url]}))
        await db.commit()
    api.on("/fake/feed.xml", ok(fixture("feed", "rss.xml")))
    c = await client_for("alice")
    body = {"name": "Brief", "agent": "morning-brief", "cron": "0 8 * * 1-5", "inputs": {"feeds": [url]}}
    s = (await c.post("/api/schedules", json=body)).json()
    run_id = (await c.post(f"/api/schedules/{s['id']}/run")).json()["run_id"]
    set_model_override(None)  # through app.llm.client to the in-process fake gateway
    assert await execute_run(uuid.UUID(run_id)) == "succeeded"
    detail = (await c.get(f"/api/runs/{run_id}")).json()
    assert detail["report"]["status"] == "alert" and detail["fingerprints"]
    assert all(f["verified"] for f in detail["report"]["findings"])
    items = (await c.get("/api/inbox")).json()["items"]
    assert items[0]["kind"] == "alert" and items[0]["link"] == f"/runs/{run_id}"
