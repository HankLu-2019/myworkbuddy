"""The outcome dashboard (design 08 §F9; plan F9): figures from a seeded fixture, exactly."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import update

from app.db import get_sessionmaker
from app.models import Approval, Integration, JiraIssue, Job, JobEvent, Plan, PlanRevision, Project, User, WorkItem

T = datetime  # all seeded instants are UTC; alice lives in New York (UTC-4 in October)
M = timedelta(minutes=1)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return T(2026, 10, day, hour, minute, tzinfo=UTC)


async def _job(db, owner: User, project: Project, integration_id: int, *, key: str, issue_type: str, created: datetime,
               status: str, cost: str, revisions: list[tuple[str | None, datetime]], ran: int, events: list[tuple],
               error_code: str | None = None, review_round: int = 0, started: bool = True) -> None:  # fmt: skip
    issue = JiraIssue(owner_id=owner.id, integration_id=integration_id, key=key, summary=key, status="To Do",
                      issue_type=issue_type, updated_remote=created, fields={})  # fmt: skip
    db.add(issue)
    await db.flush()
    item = WorkItem(owner_id=owner.id, jira_issue_id=issue.id, project_id=project.id, state="approved")
    db.add(item)
    await db.flush()
    plan = Plan(work_item_id=item.id, owner_id=owner.id, status="approved", current_revision=len(revisions))
    db.add(plan)
    await db.flush()
    revs = []
    for n, (rev_cost, when) in enumerate(revisions, start=1):
        digest = hashlib.sha256(f"{key}{n}".encode()).hexdigest()
        price = Decimal(rev_cost) if rev_cost else None
        rev = PlanRevision(plan_id=plan.id, revision=n, content={}, content_hash=digest, author="planner",
                           validation={}, cost_usd=price, created_at=when)  # fmt: skip
        db.add(rev)
        revs.append(rev)
    await db.flush()
    approval = Approval(owner_id=owner.id, subject_type="plan_revision", subject_id=revs[ran - 1].id,
                        payload_hash="0" * 64, decision="approved")  # fmt: skip
    db.add(approval)
    await db.flush()
    job = Job(owner_id=owner.id, work_item_id=item.id, plan_revision_id=revs[ran - 1].id, plan_approval_id=approval.id,
              project_id=project.id, base_branch="main", base_sha="a" * 40, branch_name=f"mwb/{key}", status=status,
              policy={}, cost_usd=Decimal(cost), error_code=error_code, review_round=review_round, created_at=created,
              started_at=created if started else None)  # fmt: skip
    db.add(job)
    await db.flush()
    for seq, (offset, frm, to) in enumerate(events, start=1):
        db.add(JobEvent(job_id=job.id, seq=seq, at=created + offset, type="status", data={"from": frm, "to": to}))


async def seed(make_user) -> dict:
    alice = await make_user("alice")
    bob = await make_user("bob")
    await make_user("root", role="admin")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(timezone="America/New_York"))
        jira = Integration(kind="jira", name="Jira", base_url="https://jira.example.com")
        db.add(jira)
        await db.flush()
        pay = Project(owner_id=alice.id, visibility="private", name="payments", host="gitlab", status="ready",
                      repo_url="https://g/team/payments.git")  # fmt: skip
        ledger = Project(owner_id=alice.id, visibility="private", name="ledger", host="gitlab", status="ready",
                         repo_url="https://g/team/ledger.git")  # fmt: skip
        theirs = Project(owner_id=bob.id, visibility="private", name="search", host="gitlab", status="ready",
                         repo_url="https://g/team/search.git")  # fmt: skip
        db.add_all([pay, ledger, theirs])
        await db.flush()
        j = jira.id
        review = [(1 * M, "preparing", "running"), (20 * M, "running", "verifying"), (25 * M, "verifying", "running"),
                  (40 * M, "running", "verifying"), (60 * M, "verifying", "awaiting_review"),
                  (120 * M, "awaiting_review", "approved_push"), (180 * M, "pushing", "pushed"),
                  (27 * 60 * M, "pushed", "merged")]  # fmt: skip
        # J1 merged: 2 attempts, 60 min to review, 24 h from push to merge, the second plan revision.
        await _job(db, alice, pay, j, key="PAY-1", issue_type="Bug", created=at(5, 14), status="merged", cost="1.50",
                   revisions=[("0.10", at(5, 13)), ("0.20", at(5, 13, 30))], ran=2, events=review)  # fmt: skip
        # J2 merged after a second round: 30 min to the first review, first push → merge 240 min.
        await _job(db, alice, pay, j, key="PAY-2", issue_type="Story", created=at(13, 15), status="merged",
                   cost="2.50", revisions=[("0.30", at(13, 14))], ran=1, review_round=1, events=[
                       (1 * M, "preparing", "running"), (30 * M, "verifying", "awaiting_review"),
                       (60 * M, "pushing", "pushed"), (120 * M, "pushed", "preparing"),
                       (121 * M, "preparing", "running"), (160 * M, "verifying", "awaiting_review"),
                       (180 * M, "pushing", "pushed"), (300 * M, "pushed", "merged")])  # fmt: skip
        # J3 failed after a question (the answer resumes the same attempt).
        await _job(db, alice, pay, j, key="PAY-3", issue_type="Bug", created=at(6, 12), status="failed", cost="0.40",
                   error_code="verify_failed", revisions=[("0.05", at(6, 11))], ran=1, events=[
                       (1 * M, "preparing", "running"), (5 * M, "running", "needs_input"),
                       (10 * M, "needs_input", "running"), (15 * M, "running", "failed")])  # fmt: skip
        # J4 timed out, in another repository, on the third revision.
        await _job(db, alice, ledger, j, key="LED-1", issue_type="Bug", created=at(14, 12), status="timed_out",
                   cost="0.60", revisions=[("0.10", at(14, 11))] * 3, ran=3,
                   events=[(1 * M, "preparing", "running"), (90 * M, "running", "timed_out")])  # fmt: skip
        # J5 still in review.
        await _job(db, alice, pay, j, key="PAY-5", issue_type="Bug", created=at(16, 12), status="awaiting_review",
                   cost="0.20", revisions=[(None, at(16, 11))], ran=1,
                   events=[(1 * M, "preparing", "running"), (45 * M, "verifying", "awaiting_review")])  # fmt: skip
        # J6 cancelled before it started: 19 Oct 03:30 UTC is still 18 Oct in New York, so it's in the period.
        await _job(db, alice, pay, j, key="PAY-6", issue_type="Bug", created=at(19, 3, 30), status="cancelled",
                   cost="0", revisions=[("0", at(19, 3))], ran=1, started=False,
                   events=[(1 * M, "created", "cancelled")])  # fmt: skip
        # J7: 5 Oct 03:00 UTC is 4 Oct in New York: before the period.
        await _job(db, alice, pay, j, key="PAY-7", issue_type="Bug", created=at(5, 3), status="merged", cost="9.99",
                   revisions=[("1.00", at(5, 2))], ran=1, events=review)  # fmt: skip
        # Bob's merged job: only admins see it, with scope=all.
        await _job(db, bob, theirs, j, key="SRC-1", issue_type="Bug", created=at(7, 12), status="merged", cost="5.00",
                   revisions=[("0.50", at(7, 12))], ran=1, events=review)  # fmt: skip
        await db.commit()
    return {"alice": alice, "pay": pay.id, "ledger": ledger.id}


PERIOD = {"from": "2026-10-05", "to": "2026-10-18"}


async def test_figures_match_the_seeded_fixture(make_user, client_for) -> None:
    await seed(make_user)
    c = await client_for("alice", create=False)
    r = await c.get("/api/insights", params=PERIOD)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["start"], d["end"], d["timezone"], d["scope"]) == ("2026-10-05", "2026-10-18", "America/New_York", "me")
    assert (d["jobs"], d["merged"]) == (6, 2)
    assert d["outcomes"] == [
        {"outcome": "merged", "error_code": None, "jobs": 2, "cost_usd": 4.0},
        {"outcome": "cancelled", "error_code": None, "jobs": 1, "cost_usd": 0.0},
        {"outcome": "failed", "error_code": "verify_failed", "jobs": 1, "cost_usd": 0.4},
        {"outcome": "in_progress", "error_code": None, "jobs": 1, "cost_usd": 0.2},
        {"outcome": "timed_out", "error_code": None, "jobs": 1, "cost_usd": 0.6},
    ]
    assert d["median_minutes_to_review"] == 45.0  # 60, 30, 45
    assert d["median_minutes_push_to_merge"] == 840.0  # 1440, 240
    assert d["attempts"] == {"mean": 1.17, "distribution": [
        {"value": 0, "label": "0", "jobs": 1}, {"value": 1, "label": "1", "jobs": 3},
        {"value": 2, "label": "2", "jobs": 2}]}  # fmt: skip
    assert d["review_rounds"] == {"mean": 1.17, "distribution": [
        {"value": 1, "label": "1", "jobs": 5}, {"value": 2, "label": "2", "jobs": 1}]}  # fmt: skip
    assert d["plan_revisions"] == {"mean": 1.5, "distribution": [
        {"value": 1, "label": "1", "jobs": 4}, {"value": 2, "label": "2", "jobs": 1},
        {"value": 3, "label": "3", "jobs": 1}]}  # fmt: skip
    assert d["cost"] == {"jobs_usd": 5.2, "planning_usd": 0.95, "total_usd": 6.15, "per_merged_usd": 2.3}
    assert d["weeks"] == [
        {"week_start": "2026-10-05", "jobs": 2, "merged": 1, "job_cost_usd": 1.9, "planning_cost_usd": 0.35,
         "cost_usd": 2.25},
        {"week_start": "2026-10-12", "jobs": 4, "merged": 1, "job_cost_usd": 3.3, "planning_cost_usd": 0.6,
         "cost_usd": 3.9},
    ]  # fmt: skip


async def test_filters_scope_and_the_period_check(make_user, client_for) -> None:
    s = await seed(make_user)
    c = await client_for("alice", create=False)
    ledger = (await c.get("/api/insights", params=PERIOD | {"project_id": s["ledger"]})).json()
    assert (ledger["jobs"], [o["outcome"] for o in ledger["outcomes"]]) == (1, ["timed_out"])
    story = (await c.get("/api/insights", params=PERIOD | {"issue_type": "Story"})).json()
    assert (story["jobs"], story["merged"], story["cost"]["per_merged_usd"]) == (1, 1, 2.8)
    r = await c.get("/api/insights", params=PERIOD | {"scope": "all"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert (await c.get("/api/insights/options", params={"scope": "all"})).status_code == 403
    opts = (await c.get("/api/insights/options")).json()
    assert opts == {"projects": [{"id": s["ledger"], "name": "ledger"}, {"id": s["pay"], "name": "payments"}],
                    "issue_types": ["Bug", "Story"]}  # fmt: skip
    root = await client_for("root", create=False)
    everyone = (await root.get("/api/insights", params=PERIOD | {"scope": "all"})).json()
    # The period is read in the viewer's time zone (Stockholm for root): PAY-7 is in, PAY-6 is out, SRC-1 is added.
    assert (everyone["scope"], everyone["jobs"], everyone["merged"]) == ("all", 7, 4)
    assert {p["name"] for p in (await root.get("/api/insights/options", params={"scope": "all"})).json()["projects"]} \
        == {"ledger", "payments", "search"}  # fmt: skip
    bad = await c.get("/api/insights", params={"from": "2026-10-18", "to": "2026-10-05"})
    assert bad.status_code == 422 and bad.json()["field"] == "from"
    assert (await c.get("/api/insights", params={"from": "2025-01-01", "to": "2026-10-18"})).status_code == 422


async def test_an_empty_period_has_no_figures(make_user, client_for) -> None:
    await seed(make_user)
    c = await client_for("alice", create=False)
    d = (await c.get("/api/insights", params={"from": "2026-09-01", "to": "2026-09-07"})).json()
    assert (d["jobs"], d["merged"], d["outcomes"]) == (0, 0, [])
    assert d["median_minutes_to_review"] is None and d["median_minutes_push_to_merge"] is None
    assert d["attempts"] == {"mean": None, "distribution": []}
    assert d["cost"] == {"jobs_usd": 0.0, "planning_usd": 0.0, "total_usd": 0.0, "per_merged_usd": None}
    assert [w["week_start"] for w in d["weeks"]] == ["2026-08-31", "2026-09-07"]  # local Mondays
    assert all(w["jobs"] == 0 and w["cost_usd"] == 0 for w in d["weeks"])
    default = (await c.get("/api/insights")).json()  # the last 30 local days
    start, end = (datetime.fromisoformat(default[k]).date() for k in ("start", "end"))
    assert (end - start).days == 29
