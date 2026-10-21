"""Schedules, the tick and runs (design 02 §4; plan S4)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update

from app.agents.executor import RunOutcome
from app.agents.seeds import sync_system_agents
from app.config import get_settings
from app.db import get_sessionmaker
from app.knowledge.episodes import build_episode, sessions_needing_episodes
from app.models import AuditLog, ChatSession, InboxItem, Message, OutboundEmail, Run, Schedule
from app.schedules import cron, service
from app.schedules.service import decide_notify, execute_run, tick
from app.tasks import app as tasks

STHLM = ZoneInfo("Europe/Stockholm")
JQL = {"jql": "project = SPIN"}


@pytest.fixture(autouse=True)
async def agents() -> None:
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        await db.commit()


async def _create(c, **overrides) -> dict:
    body = {"name": "Digest", "agent": "jira-digest", "cron": "0 8 * * 1-5", "inputs": JQL} | overrides
    r = await c.post("/api/schedules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _due(schedule_id: str, at: datetime) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(update(Schedule).where(Schedule.id == uuid.UUID(schedule_id)).values(next_run_at=at))
        await db.commit()


async def _runs(schedule_id: str | None = None) -> list[Run]:
    async with get_sessionmaker()() as db:
        q = select(Run).order_by(Run.created_at)
        if schedule_id:
            q = q.where(Run.schedule_id == uuid.UUID(schedule_id))
        return list(await db.scalars(q))


def outcome(fingerprints: list[str], status: str = "alert", **kw) -> RunOutcome:
    findings = [{"fingerprint": f, "severity": status if status != "ok" else "info", "title": f"T {f}", "detail": "",
                 "links": [], "verified": True} for f in fingerprints]  # fmt: skip
    report = {"schema_version": 1, "status": status, "headline": f"{len(findings)} issues", "findings": findings,
              "summary_md": "s"}  # fmt: skip
    return RunOutcome(
        status="succeeded", report=report, fingerprints=sorted(fingerprints), cost_usd=Decimal("0.01"), **kw
    )


def failure(code: str = "llm_unavailable", status: str = "failed") -> RunOutcome:
    return RunOutcome(status=status, error_code=code, error=f"{code} happened")  # type: ignore[arg-type]


@pytest.fixture
def results(monkeypatch) -> list[RunOutcome]:
    """Queue outcomes for execute_agent_run; each run takes the next one."""
    queue: list[RunOutcome] = []

    async def fake(agent, user, **kwargs) -> RunOutcome:
        return queue.pop(0)

    monkeypatch.setattr(service, "execute_agent_run", fake)
    return queue


async def _fire(schedule_id: str, at: datetime) -> Run:
    """Make the schedule due, tick, and execute the run it created."""
    await _due(schedule_id, at)
    created = (await tick(at)).created
    assert len(created) == 1
    await execute_run(created[0][0])
    async with get_sessionmaker()() as db:
        return await db.get(Run, created[0][0])


# ---------- cron ----------


def test_spring_forward_skips_the_missing_time() -> None:
    times = cron.occurrences("30 2 * * *", STHLM, datetime(2026, 3, 27, 12, tzinfo=UTC), 3)
    assert [t.astimezone(STHLM).isoformat() for t in times] == [
        "2026-03-28T02:30:00+01:00",
        "2026-03-30T02:30:00+02:00",  # 2026-03-29 02:30 doesn't exist in Stockholm
        "2026-03-31T02:30:00+02:00",
    ]
    hourly = cron.occurrences("0 * * * *", STHLM, datetime(2026, 3, 29, 0, 30, tzinfo=UTC), 2)
    assert [t.astimezone(STHLM).hour for t in hourly] == [3, 4] and hourly[1] - hourly[0] == timedelta(hours=1)


def test_fall_back_fires_once_at_the_first_occurrence() -> None:
    times = cron.occurrences("30 2 * * *", STHLM, datetime(2026, 10, 24, 12, tzinfo=UTC), 2)
    assert times[0] == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST, not 02:30 CET an hour later
    assert times[1].astimezone(STHLM).isoformat() == "2026-10-26T02:30:00+01:00"
    # Asked from inside the repeated hour, the second 02:30 is not a new fire time.
    assert cron.next_after("30 2 * * *", "Europe/Stockholm", datetime(2026, 10, 25, 1, 0, tzinfo=UTC)) == times[1]
    quarter = cron.occurrences("*/15 * * * *", STHLM, datetime(2026, 10, 24, 23, 50, tzinfo=UTC), 12)
    assert len(set(quarter)) == 12 and quarter == sorted(quarter)
    assert all(t.astimezone(STHLM).replace(tzinfo=None) for t in quarter)


@pytest.mark.parametrize(
    ("expr", "tz", "error"),
    [
        ("0 8 * *", "UTC", "Use 5 fields"),
        ("0 8 * * * *", "UTC", "Use 5 fields"),
        ("0 25 * * *", "UTC", "not a valid cron expression"),
        ("0 8 31 2 *", "UTC", "never fires"),
        ("*/5 * * * *", "UTC", "5 minutes apart; the minimum is 15"),
        ("0 8 * * *", "Mars/Base", "Unknown timezone"),
    ],
)
def test_preview_errors(expr: str, tz: str, error: str) -> None:
    p = cron.preview(expr, tz, min_interval_min=15, now=datetime(2026, 10, 21, tzinfo=UTC))
    assert not p.valid and error in p.error


async def test_preview_endpoint_uses_the_users_timezone_and_role_minimum(client_for) -> None:
    c = await client_for("alice")
    r = (await c.post("/api/schedules/preview", json={"cron": "0  8 * * 1-5"})).json()
    assert r["valid"] and r["description"] == "At 08:00, Monday through Friday"
    assert len(r["next"]) == 5 and all(t.endswith(("+01:00", "+02:00")) for t in r["next"])  # Europe/Stockholm
    assert not (await c.post("/api/schedules/preview", json={"cron": "*/10 * * * *"})).json()["valid"]
    admin = await client_for("root", role="admin")
    assert (await admin.post("/api/schedules/preview", json={"cron": "*/10 * * * *"})).json()["valid"]
    assert not (await admin.post("/api/schedules/preview", json={"cron": "*/2 * * * *"})).json()["valid"]


# ---------- the API ----------


async def test_create_read_update_delete(client_for) -> None:
    c = await client_for("alice")
    s = await _create(c, cron="0  8 * * 1-5", prompt="Only blockers")
    assert s["cron"] == "0 8 * * 1-5" and s["timezone"] == "Europe/Stockholm" and s["agent"]["slug"] == "jira-digest"
    assert s["description"] == "At 08:00, Monday through Friday" and s["next_run_at"]
    local = datetime.fromisoformat(s["next_run_at"]).astimezone(STHLM)
    assert (local.hour, local.minute) == (8, 0) and local.weekday() < 5

    r = await c.patch(f"/api/schedules/{s['id']}", json={"cron": "30 9 * * *", "timezone": "UTC"})
    assert r.status_code == 200 and datetime.fromisoformat(r.json()["next_run_at"]).hour == 9
    r = await c.patch(f"/api/schedules/{s['id']}", json={"enabled": False})
    assert r.json()["next_run_at"] is None
    assert (await c.get(f"/api/schedules/{s['id']}")).json()["enabled"] is False

    bob = await client_for("bob")
    for method, path in [("get", ""), ("patch", ""), ("delete", ""), ("post", "/run"), ("get", "/runs")]:
        kwargs = {"json": {}} if method == "patch" else {}
        assert (await getattr(bob, method)(f"/api/schedules/{s['id']}{path}", **kwargs)).status_code == 404

    assert (await c.delete(f"/api/schedules/{s['id']}")).status_code == 204
    assert (await c.get(f"/api/schedules/{s['id']}")).status_code == 404
    async with get_sessionmaker()() as db:
        actions = list(
            await db.scalars(select(AuditLog.action).where(AuditLog.target_type == "schedule").order_by(AuditLog.id))
        )
    assert actions == ["schedule.create", "schedule.update", "schedule.update", "schedule.delete"]


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"agent": "nope"}, "agent", "no agent called 'nope'"),
        ({"inputs": {}}, "inputs", "'jql' is a required property"),
        ({"inputs": {"jql": "x", "colour": "red"}}, "inputs", "Additional properties"),
        ({"inputs": {"jql": ""}}, "inputs.jql", "jql:"),
        ({"cron": "*/5 * * * *"}, "cron", "the minimum is 15 minutes"),
        ({"timezone": "Mars/Base"}, "timezone", "Unknown timezone"),
        ({"name": "   "}, "name", "1–120 characters"),
    ],
)
async def test_validation(client_for, body: dict, field: str, message: str) -> None:
    c = await client_for("alice")
    r = await c.post(
        "/api/schedules", json={"name": "x", "agent": "jira-digest", "cron": "0 8 * * *", "inputs": JQL} | body
    )
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    assert r.json()["field"] == field and message in r.json()["detail"]


async def test_per_user_limit_counts_enabled_schedules(client_for, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_schedules_per_user", 2)
    c = await client_for("alice")
    first = await _create(c)
    await _create(c)
    r = await c.post("/api/schedules", json={"name": "3", "agent": "jira-digest", "cron": "0 8 * * *", "inputs": JQL})
    assert r.status_code == 409 and "at most 2 active schedules" in r.json()["detail"]
    await _create(c, enabled=False)  # paused ones don't count
    await c.patch(f"/api/schedules/{first['id']}", json={"enabled": False})
    await _create(c, name="room again")
    r = await c.patch(f"/api/schedules/{first['id']}", json={"enabled": True})
    assert r.status_code == 409
    assert (await c.patch(f"/api/schedules/{first['id']}", json={"name": "renamed"})).status_code == 200


async def test_list_is_cursor_paginated(client_for) -> None:
    c = await client_for("alice")
    ids = [(await _create(c, name=f"s{i}"))["id"] for i in range(5)]
    page1 = (await c.get("/api/schedules", params={"limit": 2})).json()
    page2 = (await c.get("/api/schedules", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    page3 = (await c.get("/api/schedules", params={"limit": 2, "cursor": page2["next_cursor"]})).json()
    seen = [s["id"] for p in (page1, page2, page3) for s in p["items"]]
    assert seen == ids[::-1] and page3["next_cursor"] is None
    assert (await c.get("/api/schedules", params={"cursor": "garbage"})).status_code == 400


# ---------- the tick ----------


async def test_concurrent_ticks_create_one_run_per_fire_time(client_for) -> None:
    c = await client_for("alice")
    s = await _create(c)
    fire = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    await _due(s["id"], fire)
    results = await asyncio.gather(*(tick(fire + timedelta(seconds=5)) for _ in range(4)))
    assert sum(len(r.created) for r in results) == 1
    [run] = await _runs(s["id"])
    assert run.scheduled_for == fire and run.status == "queued" and run.trigger == "schedule"
    assert run.config["inputs"] == JQL and run.config["agent_slug"] == "jira-digest"
    # Even if next_run_at were rolled back, the unique (schedule, fire time) key stops a second run.
    await _due(s["id"], fire)
    assert (await tick(fire + timedelta(seconds=10))).created == []
    assert len(await _runs(s["id"])) == 1


async def test_missed_fires_produce_at_most_one_catch_up_run(client_for) -> None:
    c = await client_for("alice")
    s = await _create(c, cron="0 * * * *")
    at = datetime(2026, 10, 21, 12, 10, tzinfo=UTC)
    await _due(s["id"], at - timedelta(days=2))  # the worker was down for two days
    assert len((await tick(at)).created) == 1
    assert (await tick(at + timedelta(seconds=30))).created == []
    async with get_sessionmaker()() as db:
        sched = await db.get(Schedule, uuid.UUID(s["id"]))
    assert sched.next_run_at == datetime(2026, 10, 21, 13, 0, tzinfo=UTC)


async def test_tick_skips_inactive_owners(client_for) -> None:
    c = await client_for("alice")
    s = await _create(c)
    t = await _create(c, name="other")
    at = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    await _due(s["id"], at)
    await _due(t["id"], at)
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Schedule).where(Schedule.id == uuid.UUID(t["id"])).values(owner_id=(await _inactive(db)))
        )
        await db.commit()
    result = await tick(at + timedelta(seconds=1))
    assert len(result.created) == 1 and result.disabled == 1
    async with get_sessionmaker()() as db:
        other = await db.get(Schedule, uuid.UUID(t["id"]))
    assert other.enabled is False and other.next_run_at is None and "deactivated" in other.paused_reason


async def _inactive(db) -> int:
    from app.auth.service import hash_password
    from app.models import User

    u = User(username="gone", password_hash=hash_password("x" * 12), is_active=False)
    db.add(u)
    await db.flush()
    return u.id


async def test_stale_queued_runs_are_redeferred_without_duplicates(client_for, task_queue, monkeypatch) -> None:
    c = await client_for("alice")
    s = await _create(c)
    at = datetime.now(UTC)
    await _due(s["id"], at - timedelta(minutes=1))
    monkeypatch.setattr(service, "now", lambda: at)
    await tasks.schedule_tick(timestamp=0)  # the periodic task's body
    assert _jobs(task_queue, "run_schedule") == 1
    task_queue.reset()  # the deferred job was lost, and the run is still queued 6 minutes later
    async with get_sessionmaker()() as db:
        await db.execute(update(Run).values(created_at=at - timedelta(minutes=6)))
        await db.commit()
    for _ in range(2):
        await tasks.schedule_tick(timestamp=0)
    assert _jobs(task_queue, "run_schedule") == 1  # re-deferred once; the queueing lock stops the second
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "run_schedule"]
    assert job["lock"] == f"schedule:{s['id']}" and job["queue_name"] == "llm"
    assert len(await _runs(s["id"])) == 1


def _jobs(q, name: str) -> int:
    return sum(1 for j in q.jobs.values() if j["task_name"] == name and j["status"] == "todo")


async def test_runs_stuck_in_running_are_failed(client_for) -> None:
    c = await client_for("alice")
    s = await _create(c)
    at = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    await _due(s["id"], at)
    [(run_id, _)] = (await tick(at)).created
    async with get_sessionmaker()() as db:
        await db.execute(update(Run).values(status="running", started_at=at))
        await db.commit()
    assert (await tick(at + timedelta(minutes=5))).interrupted == 0
    assert (await tick(at + timedelta(seconds=300 + 601))).interrupted == 1
    [run] = await _runs(s["id"])
    assert run.status == "failed" and run.error_code == "interrupted"


# ---------- executing runs ----------


async def test_a_run_stores_the_report_and_its_transcript_session(client_for, results) -> None:
    c = await client_for("alice")
    s = await _create(c, notify_mode="always")
    results.append(outcome(["jira:SPIN-1"], tokens_in=10, tokens_out=5))
    run = await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    assert run.status == "succeeded" and run.fingerprints == ["jira:SPIN-1"] and run.changed is True
    assert run.cost_usd == Decimal("0.01") and run.tokens_in == 10 and run.notified
    async with get_sessionmaker()() as db:
        session = await db.get(ChatSession, run.session_id)
        sched = await db.get(Schedule, uuid.UUID(s["id"]))
    assert session.source == "run" and session.title == "Digest · 2026-10-21 08:00"  # owner's local time
    assert sched.last_status == "alert" and sched.last_run_at == run.finished_at
    detail = (await c.get(f"/api/runs/{run.id}")).json()
    assert detail["report"]["headline"] == "1 issues" and detail["session_id"] == str(run.session_id)
    assert detail["inputs"] == JQL and detail["schedule_name"] == "Digest" and detail["report_status"] == "alert"
    page = (await c.get(f"/api/schedules/{s['id']}/runs")).json()
    assert [r["id"] for r in page["items"]] == [str(run.id)] and page["next_cursor"] is None
    bob = await client_for("bob")
    assert (await bob.get(f"/api/runs/{run.id}")).status_code == 404
    assert str(run.session_id) not in str((await c.get("/api/chat/sessions")).json())  # not in the chat list
    assert (await c.post(f"/api/chat/sessions/{run.session_id}/messages", json={"content": "hi"})).status_code == 409


@pytest.mark.parametrize(
    ("mode", "status_", "changed", "previous", "current", "kind"),
    [
        ("always", "ok", False, None, [], "run"),
        ("always", "alert", False, ["a"], ["a"], "alert"),
        ("always", "ok", True, ["a"], [], "recovered"),
        ("on_change", "warning", True, None, ["a"], "alert"),  # first run with findings
        ("on_change", "ok", True, None, [], None),  # first run, all fine: nothing to say
        ("on_change", "alert", False, ["a"], ["a"], None),  # same problem as last time
        ("on_change", "alert", True, ["a"], ["a", "b"], "alert"),  # a new problem
        ("on_change", "ok", True, ["a"], [], "recovered"),
        ("on_change", "ok", False, [], [], None),
        ("on_change", "warning", True, ["a"], [], "alert"),  # unverified findings left: not "recovered"
        ("on_alert", "warning", True, None, ["a"], None),
        ("on_alert", "alert", False, ["a"], ["a"], None),
        ("on_alert", "alert", True, ["a"], ["b"], "alert"),
        ("on_alert", "ok", True, ["a"], [], None),
    ],
)
def test_notify_mode_table(mode, status_, changed, previous, current, kind) -> None:
    assert decide_notify(mode, status_, changed, previous, current) == kind


async def test_on_change_alerts_stays_quiet_and_recovers(client_for, make_user, results) -> None:
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        from app.models import User

        await db.execute(update(User).where(User.username == "alice").values(email="alice@example.com"))
        await db.commit()
    s = await _create(c)
    day = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    results.extend([outcome(["jira:SPIN-1"]), outcome(["jira:SPIN-1"]), outcome([], status="ok")])
    first = await _fire(s["id"], day)
    second = await _fire(s["id"], day + timedelta(days=1))
    third = await _fire(s["id"], day + timedelta(days=2))
    assert (first.changed, first.notified) == (True, True)
    assert (second.changed, second.notified) == (False, False)
    assert (third.changed, third.notified) == (True, True)
    async with get_sessionmaker()() as db:
        items = list(await db.scalars(select(InboxItem).order_by(InboxItem.id)))
        emails = list(await db.scalars(select(OutboundEmail).order_by(OutboundEmail.id)))
    assert [(i.kind, i.severity, i.run_id) for i in items] == [
        ("alert", "alert", first.id),
        ("recovered", "info", third.id),
    ]
    assert items[0].title == "Digest: 1 issues" and items[0].link == f"/runs/{first.id}"
    assert [e.dedupe_key for e in emails] == [f"run:{first.id}", f"run:{third.id}"]
    assert emails[0].to_addr == "alice@example.com" and f"/runs/{first.id}" in emails[0].body_text


async def test_emails_carry_no_links_from_findings(client_for, results, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "hostname", "https://mwb.example.com")
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        from app.models import User

        await db.execute(update(User).values(email="alice@example.com"))
        await db.commit()
    s = await _create(c)
    evil = outcome(["jira:SPIN-1"])
    evil.report["findings"][0]["title"] = "Reset your password at https://evil.example/login now"
    evil.report["headline"] = "See www.evil.example"
    results.append(evil)
    run = await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        [email] = list(await db.scalars(select(OutboundEmail)))
    assert "evil.example" not in email.body_text + email.subject and "[link removed]" in email.body_text
    links = [w for w in email.body_text.split() if "://" in w]
    assert links == [f"https://mwb.example.com/runs/{run.id}"]


async def test_no_email_when_the_user_turned_it_off_or_the_schedule_did(client_for, results) -> None:
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        from app.models import User

        await db.execute(update(User).values(email="alice@example.com", email_notifications=False))
        await db.commit()
    s = await _create(c, notify_mode="always")
    t = await _create(c, name="quiet", notify_mode="always", notify_email=False)
    results.extend([outcome(["jira:SPIN-1"]), outcome(["jira:SPIN-1"])])
    await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        await db.execute(update(User).values(email_notifications=True))
        await db.commit()
    await _fire(t["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(OutboundEmail)) == 0
        assert await db.scalar(select(func.count()).select_from(InboxItem)) == 2


async def test_three_failures_pause_with_inbox_and_email(client_for, results) -> None:
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        from app.models import User

        await db.execute(update(User).values(email="alice@example.com"))
        await db.commit()
    s = await _create(c)
    day = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    results.extend([failure(), failure("timeout", "timed_out"), failure()])
    for i in range(2):
        run = await _fire(s["id"], day + timedelta(days=i))
    assert run.status == "timed_out"
    got = (await c.get(f"/api/schedules/{s['id']}")).json()
    assert got["enabled"] and got["consecutive_failures"] == 2 and got["last_status"] == "timed_out"
    await _fire(s["id"], day + timedelta(days=2))
    got = (await c.get(f"/api/schedules/{s['id']}")).json()
    assert not got["enabled"] and got["next_run_at"] is None
    assert got["paused_reason"] == "Schedule Digest was paused after 3 failures: llm_unavailable happened"
    async with get_sessionmaker()() as db:
        [item] = list(await db.scalars(select(InboxItem)))
        [email] = list(await db.scalars(select(OutboundEmail)))
    assert item.kind == "system" and item.title == "Schedule Digest was paused" and item.link == f"/schedules/{s['id']}"
    assert email.subject == "Schedule Digest was paused" and "after 3 failures" in email.body_text
    # Resuming clears the pause.
    got = (await c.patch(f"/api/schedules/{s['id']}", json={"enabled": True})).json()
    assert got["enabled"] and got["consecutive_failures"] == 0 and got["paused_reason"] is None and got["next_run_at"]


async def test_a_success_resets_the_failure_count(client_for, results) -> None:
    c = await client_for("alice")
    s = await _create(c)
    day = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    results.extend([failure(), failure(), outcome([], status="ok"), failure(), failure()])
    for i in range(5):
        await _fire(s["id"], day + timedelta(days=i))
    got = (await c.get(f"/api/schedules/{s['id']}")).json()
    assert got["enabled"] and got["consecutive_failures"] == 2


async def test_budget_exceeded_pauses_immediately(client_for, results) -> None:
    c = await client_for("alice")
    s = await _create(c)
    results.append(failure("budget_exceeded"))
    await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    got = (await c.get(f"/api/schedules/{s['id']}")).json()
    assert not got["enabled"] and "AI budget is used up" in got["paused_reason"]
    async with get_sessionmaker()() as db:
        assert (await db.scalar(select(InboxItem))).kind == "system"


async def test_runs_are_skipped_when_the_schedule_was_paused_or_deleted(client_for, results) -> None:
    c = await client_for("alice")
    s = await _create(c)
    t = await _create(c, name="gone")
    at = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    await _due(s["id"], at)
    await _due(t["id"], at)
    created = (await tick(at)).created
    await c.patch(f"/api/schedules/{s['id']}", json={"enabled": False})
    await c.delete(f"/api/schedules/{t['id']}")
    for run_id, _ in created:
        assert await execute_run(run_id) is None
    runs = await _runs()
    assert sorted(r.error for r in runs) == ["The schedule is paused.", "The schedule was deleted."]
    assert {r.status for r in runs} == {"skipped"} and results == []
    assert await execute_run(runs[0].id) is None  # idempotent: only a queued run starts


async def test_manual_run_notifies_in_app_only(client_for, results, task_queue) -> None:
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        from app.models import User

        await db.execute(update(User).values(email="alice@example.com"))
        await db.commit()
    s = await _create(c, notify_mode="on_alert", enabled=False)  # a paused schedule can still be run by hand
    r = await c.post(f"/api/schedules/{s['id']}/run")
    assert r.status_code == 202
    run_id = uuid.UUID(r.json()["run_id"])
    assert _jobs(task_queue, "run_schedule") == 1
    results.append(outcome([], status="ok"))
    assert await execute_run(run_id) == "succeeded"
    async with get_sessionmaker()() as db:
        [item] = list(await db.scalars(select(InboxItem)))
        assert await db.scalar(select(func.count()).select_from(OutboundEmail)) == 0
        run = await db.get(Run, run_id)
    assert item.kind == "run" and run.trigger == "manual" and run.notified


async def test_a_test_run_without_a_schedule_never_notifies(make_user, results) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        from app.models import Agent

        agent = await db.scalar(select(Agent).where(Agent.slug == "jira-digest"))
        run = Run(owner_id=alice.id, agent_id=agent.id, trigger="manual", status="queued",
                  config={"name": "Test", "agent_slug": agent.slug, "inputs": JQL, "schedule_id": None},
                  scheduled_for=datetime.now(UTC))  # fmt: skip
        db.add(run)
        await db.commit()
    results.append(outcome(["jira:SPIN-1"]))
    assert await execute_run(run.id) == "succeeded"
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(InboxItem)) == 0


async def test_end_to_end_with_the_real_executor(client_for) -> None:
    """The run task with execute_agent_run itself (TestModel returns a valid empty report)."""
    from pydantic_ai.models.test import TestModel

    from app.llm.models import set_model_override

    c = await client_for("alice")
    s = await _create(c, notify_mode="always")
    set_model_override(TestModel(call_tools=[]))
    run = await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    assert run.status == "succeeded" and run.report["status"] in ("ok", "warning", "alert")
    async with get_sessionmaker()() as db:
        roles = [m.role for m in await db.scalars(select(Message).where(Message.session_id == run.session_id))]
    assert roles == ["user", "assistant"]


async def test_run_sessions_are_excluded_from_episodes(client_for, results) -> None:
    c = await client_for("alice")
    s = await _create(c)
    results.append(outcome([], status="ok"))
    run = await _fire(s["id"], datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    async with get_sessionmaker()() as db:
        db.add(Message(session_id=run.session_id, role="user", content="x"))
        await db.execute(update(ChatSession).values(updated_at=datetime.now(UTC) - timedelta(days=1)))
        await db.commit()
        assert run.session_id not in await sessions_needing_episodes(db)
        assert await build_episode(db, run.session_id) is None


async def test_schedule_checks_at_the_database(make_user) -> None:
    from sqlalchemy.exc import IntegrityError

    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        from app.models import Agent

        agent_id = await db.scalar(select(Agent.id).limit(1))
    for values in [{"notify_mode": "sometimes"}, {"max_runtime_s": 5}, {"name": ""}]:
        with pytest.raises(IntegrityError):
            async with get_sessionmaker()() as db:
                db.add(Schedule(owner_id=alice.id, agent_id=agent_id, name="n", cron="0 8 * * *", timezone="UTC"))
                await db.flush()
                await db.execute(update(Schedule).values(**values))
                await db.commit()
