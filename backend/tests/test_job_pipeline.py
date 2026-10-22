"""Creating a job and its prepare → run → verify loop (design 04 §5.1–5.4, §9 tests 5–6; plan X4).

The repository is the fake forge's Git server (a real clone over HTTP), the gateway is the fake one (real key
records), and the runner is tests/fake_runner.py (real git and the real mwb-verify on temp directories).
"""

import socket
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select, update

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import ApprovalDecision, ApprovalSubject
from app.integrations.service import set_credential
from app.jobs import pipeline, runner_client
from app.models import (
    Artifact,
    InboxItem,
    Integration,
    JiraIssue,
    Job,
    JobEvent,
    JobSecret,
    OutboundEmail,
    Plan,
    PlanRevision,
    Project,
    User,
    UserCredential,
    WorkItem,
)  # fmt: skip
from app.plans import approvals
from app.plans.schema import approval_payload_hash, content_hash
from app.secrets.service import encrypt
from tests import fake_runner
from tests.connector_fakes import setup_integration
from tests.fakellm import server as fake_gateway
from tests.fakellm.server import app as forge_app

KEY = "PAY-12"
VERIFY_OK = "grep -q 'Retry transient failures' app/webhooks.py"


@pytest.fixture(scope="module")
def forge_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(forge_app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    fake = fake_runner.FakeRunner(tmp_path / "runner")
    runner_client.set_transport(httpx2.ASGITransport(app=fake.app))
    monkeypatch.setattr(get_settings(), "runner_token", SecretStr("r" * 32))
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path / "data")
    yield fake
    runner_client.set_transport(None)


def plan_doc(**over) -> dict:
    return {
        "schema_version": 1, "title": "Retry webhook delivery", "summary": "Retry on 5xx.", "ambiguities": [],
        "knowledge_refs": [], "approach": "Wrap delivery in a retry.", "alternatives": [],
        "changes": [{"path": "app/webhooks.py", "action": "modify", "description": "Retry 3 times."}],
        "tests": [], "verification": [VERIFY_OK], "risks": [], "rollback": "Revert.", "estimate": "S",
        "commit_message": f"{KEY} Retry webhook delivery",
        "sandbox": {"optional_hosts": [], "cpus": 2, "memory_gb": 4, "timeout_min": 45, "max_attempts": 3}, **over,
    }  # fmt: skip


async def approved_ticket(make_user, forge_url: str, *, token: str = "glpat-e2e-token", doc: dict | None = None,
                          email: bool = True) -> dict:  # fmt: skip
    """alice, a ready repository on the forge, and an approved plan revision for PAY-12."""
    alice, jira_id = await setup_integration(make_user, "jira", "https://jira.example.com", {"token": "jira-t"})
    sha = subprocess.run(
        ["git", "ls-remote", f"{forge_url}/fake/git/team/payments.git", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.split()[0]  # fmt: skip
    content = doc or plan_doc()
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(email_notifications=email))
        gitlab = Integration(kind="gitlab", name="Forge", base_url=forge_url)
        db.add(gitlab)
        await db.flush()
        await set_credential(db, alice.id, gitlab, "token", token)
        await db.execute(update(UserCredential).where(UserCredential.integration_id == gitlab.id).values(status="ok"))
        project = Project(owner_id=alice.id, visibility="private", name="payments", host="gitlab", status="ready",
                          repo_url=f"{forge_url}/fake/git/team/payments.git", integration_id=gitlab.id)  # fmt: skip
        db.add(project)
        issue = JiraIssue(owner_id=alice.id, integration_id=jira_id, key=KEY, summary="Retry webhook delivery",
                          status="To Do", updated_remote=datetime.now(UTC), fields={})  # fmt: skip
        db.add(issue)
        await db.flush()
        item = WorkItem(owner_id=alice.id, jira_issue_id=issue.id, project_id=project.id, state="approved")
        db.add(item)
        await db.flush()
        plan = Plan(work_item_id=item.id, owner_id=alice.id, status="approved", current_revision=1, base_sha=sha,
                    base_branch="main")  # fmt: skip
        db.add(plan)
        await db.flush()
        rev = PlanRevision(plan_id=plan.id, revision=1, content=content, content_hash=content_hash(content),
                           author="planner", validation={"ok": True, "issues": [], "needs_ack": []})  # fmt: skip
        db.add(rev)
        await db.flush()
        item.current_plan_id = plan.id
        payload = approval_payload_hash(rev.content_hash, [])
        await approvals.record(db, owner_id=alice.id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
                               payload_hash=payload, decision=ApprovalDecision.approved)  # fmt: skip
        await db.commit()
    return {"user": alice, "item": item, "rev": rev, "sha": sha, "project": project}


async def start(client_for, t: dict) -> str:
    c = await client_for("alice", create=False)
    r = await c.post(f"/api/work-items/{t['item'].id}/jobs", json={"plan_revision_id": str(t["rev"].id)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def job_row(job_id: str) -> Job:
    async with get_sessionmaker()() as db:
        return await db.get(Job, uuid.UUID(job_id))


async def events(job_id: str) -> list[JobEvent]:
    async with get_sessionmaker()() as db:
        return list(
            await db.scalars(select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)).order_by(JobEvent.seq))
        )


def key_record(job_id: str) -> dict | None:
    return next((k for k in fake_gateway.KEYS.values() if k["key_alias"] == f"mwb-job-{job_id}"), None)


# ---------- create (04 §5.1) ----------


async def test_creating_a_job_makes_its_key_and_queues_the_pipeline(
    make_user, client_for, forge_url, runner, task_queue
) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    job = await job_row(job_id)
    assert job.status == "created" and job.branch_name == "mwb/PAY-12-retry-webhook-delivery"
    assert job.base_sha == t["sha"] and job.base_branch == "main" and job.gateway_key_alias == f"mwb-job-{job_id}"
    assert job.policy["max_attempts"] == 3 and job.policy["changes"] == ["app/webhooks.py"]
    key = key_record(job_id)
    assert key["models"] == ["coder", "fast"] and key["max_budget"] == 10.0 and key["duration"] == "232m"
    assert key["metadata"] == {"mwb_user": t["user"].id, "mwb_job": job_id}
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(JobSecret.ciphertext).where(JobSecret.job_id == job.id)) != key["token"]
        assert (await db.get(WorkItem, t["item"].id)).state == "in_progress"
    [queued] = [j for j in task_queue.jobs.values() if j["task_name"] == "job_advance"]
    assert queued["lock"] == f"job:{job_id}" and queued["queue_name"] == "jobs"


async def test_job_creation_preconditions(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url, token="glpat-readonly-token")
    c = await client_for("alice", create=False)
    url = f"/api/work-items/{t['item'].id}/jobs"
    body = {"plan_revision_id": str(t["rev"].id)}
    r = await c.post(url, json=body)
    assert (
        r.status_code == 409 and r.json()["code"] == "credential_invalid" and "write_repository" in r.json()["detail"]
    )
    async with get_sessionmaker()() as db:
        await db.execute(update(UserCredential).values(ciphertext=encrypt("glpat-ok")))
        await db.execute(update(Project).values(status="running"))
        await db.commit()
    r = await c.post(url, json=body)
    assert r.status_code == 409 and r.json()["field"] == "project_id"
    async with get_sessionmaker()() as db:
        await db.execute(update(Project).values(status="ready"))
        await db.execute(update(WorkItem).values(state="needs_decision"))
        await db.commit()
    assert (await c.post(url, json=body)).status_code == 409
    async with get_sessionmaker()() as db:
        await db.execute(update(WorkItem).values(state="approved"))
        await db.execute(update(PlanRevision).values(content={**t["rev"].content, "title": "Tampered"}))
        await db.commit()
    r = await c.post(url, json=body)
    assert r.status_code == 409 and "doesn't match" in r.json()["detail"]
    bob = await client_for("bob")
    assert (await bob.post(url, json=body)).status_code == 404


async def test_one_job_at_a_time(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    await start(client_for, t)
    c = await client_for("alice", create=False)
    async with get_sessionmaker()() as db:
        await db.execute(update(WorkItem).values(state="approved"))  # even if the ticket looked startable again
        await db.commit()
    r = await c.post(f"/api/work-items/{t['item'].id}/jobs", json={"plan_revision_id": str(t["rev"].id)})
    assert r.status_code == 409 and "already has a job" in r.json()["detail"]


# ---------- the loop (04 §5.2–5.4) ----------


async def test_a_job_runs_to_review(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    captured = {}

    def agent(ws: Path, env: dict):
        captured["env"] = env
        captured["git"] = {
            "remotes": subprocess.run(["git", "remote"], cwd=ws, capture_output=True, text=True).stdout,
            "hooks": sorted(p.name for p in (ws / ".git" / "hooks").iterdir()),
            "hooksPath": subprocess.run(
                ["git", "config", "core.hooksPath"], cwd=ws, capture_output=True, text=True
            ).stdout,
            "config": (ws / ".git" / "config").read_text(),
            "task": (ws / ".mwb" / "TASK.md").read_text(),
            "prompt": (ws / ".mwb" / "prompt.md").read_text(),
            "ignored": subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True).stdout,
        }
        return fake_runner.planned_change(ws, env)

    runner.agent = [agent]
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"

    g = captured["git"]
    assert g["remotes"] == "" and g["hooks"] == [] and g["hooksPath"].strip() == "/dev/null"
    assert "token" not in g["config"].lower() and "authorization" not in g["config"].lower()
    assert "- modify `app/webhooks.py`: Retry 3 times." in g["task"] and VERIFY_OK in g["task"]
    assert "Do not run `git commit`" in g["task"] and g["prompt"].startswith("Carry out the task in .mwb/TASK.md")
    assert g["ignored"] == ""  # .mwb/ is excluded: the clean checkout shows nothing
    env = captured["env"]
    assert env["ANTHROPIC_MODEL"] == "coder" and env["AGENT_MAX_TURNS"] == "60"
    assert env["ANTHROPIC_AUTH_TOKEN"] == key_record(job_id)["token"]

    job = await job_row(job_id)
    r = job.result
    assert job.attempt == 1 and job.current_run is None and not r["failed_checks"]
    assert [f["path"] for f in r["files"]] == ["app/webhooks.py"] and r["files"][0]["added"] == 2
    assert {c["name"]: c["status"] for c in r["checks"]} == {
        "no_changes": "pass", "outside_plan": "pass", "size": "pass", "binary": "pass", "file_modes": "pass",
        "ci_config": "pass", "secret_scan": "pass", "verification": "pass",
    }  # fmt: skip
    assert r["verify"][0]["command"] == VERIFY_OK and r["verify"][0]["exit_code"] == 0
    assert r["agent_notes"] == "Changed app/webhooks.py as the task asked.\n"
    async with get_sessionmaker()() as db:
        patch = await db.get(Artifact, uuid.UUID(r["patch_artifact_id"]))
        assert patch.sha256 == r["patch_sha256"] and patch.job_id == job.id
        names = sorted(
            a.key.rsplit("/", 1)[1] for a in await db.scalars(select(Artifact).where(Artifact.job_id == job.id))
        )
        inbox = await db.scalar(select(InboxItem).where(InboxItem.kind == "job"))
        email = await db.scalar(select(OutboundEmail))
    assert names == ["agent-transcript-r0-a1.jsonl", "patch-r0-a1.diff", "verify-r0-a1-1.log"]
    assert inbox.title == "Job for PAY-12 is ready for review" and inbox.link == f"/work/{t['item'].id}/job"
    assert email.subject == "Job for PAY-12 is ready for review" and "/work/" in email.body_text
    seen = [(e.type, e.data.get("to") or e.data.get("tool") or e.data.get("name")) for e in await events(job_id)]
    assert ("status", "preparing") in seen and ("agent_tool", "Bash") in seen and ("check", "verification") in seen
    assert seen[-1] == ("status", "awaiting_review")
    assert key_record(job_id) is not None and str(job.id) in runner.workspaces  # kept for review


async def test_failed_verification_retries_with_feedback_up_to_max_attempts(
    make_user, client_for, forge_url, runner
) -> None:
    doc = plan_doc(verification=["echo checking; grep -q 'never there' app/webhooks.py"],
                   sandbox={"optional_hosts": [], "cpus": 2, "memory_gb": 4, "timeout_min": 45,
                            "max_attempts": 2})  # fmt: skip
    t = await approved_ticket(make_user, forge_url, doc=doc)
    job_id = await start(client_for, t)
    feedback = []

    def agent(ws: Path, env: dict):
        if (ws / ".mwb" / "feedback.md").exists():
            feedback.append(((ws / ".mwb" / "feedback.md").read_text(), (ws / ".mwb" / "prompt.md").read_text()))
        return fake_runner.planned_change(ws, env)

    runner.agent = [agent, agent, agent]
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    job = await job_row(job_id)
    assert job.attempt == 2 and job.result["attempts"] == 2 and job.result["failed_checks"] is True
    assert len(runner.agent) == 1  # two agent runs, then it stops
    [(text, prompt)] = feedback
    assert "# Verification failed" in text and "never there" in text and "checking" in text and "exited 1" in text
    assert "previous attempt failed verification" in prompt
    notes = [e.data.get("note") for e in await events(job_id) if e.data.get("note")]
    assert "Verification failed; attempt 2 of 2." in notes
    async with get_sessionmaker()() as db:
        inbox = await db.scalar(select(InboxItem).where(InboxItem.kind == "job"))
    assert inbox.title.endswith("(checks failed)") and inbox.severity == "warning"


async def test_infrastructure_flake_is_retried_once(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    runner.agent = [lambda ws, env: (1, []), fake_runner.planned_change]
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert any(e.type == "error" for e in await events(job_id))


async def test_an_agent_failure_ends_the_job_and_cleans_up(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    lines = fake_runner.stream(fake_runner.tool("ls"), fake_runner.result(ok=False, message="API Error: 500 boom"))
    runner.agent = [lambda ws, env: (1, lines)]
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    job = await job_row(job_id)
    assert (job.failed_stage, job.error_code, job.error) == ("running", "agent_failed", "API Error: 500 boom")
    assert key_record(job_id) is None and job_id not in runner.workspaces  # key and workspace gone
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(JobSecret).where(JobSecret.job_id == job.id)) is None
        assert (await db.get(WorkItem, t["item"].id)).state == "approved"
        assert (await db.scalar(select(InboxItem).where(InboxItem.kind == "job"))).title == "Job for PAY-12 failed"


async def test_5_a_job_over_its_time_is_killed_and_timed_out(
    make_user, client_for, forge_url, runner, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline, "LOG_GRACE_S", 0.3)
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).values(policy=Job.policy.op("||")({"timeout_min": 0})))
        await db.commit()
    runner.agent = ["hang"]
    assert await pipeline.advance(uuid.UUID(job_id)) == "timed_out"
    job = await job_row(job_id)
    assert job.error_code == "timed_out" and job.finished_at is not None
    killed = [r for r in runner.runs.values() if r["state"] == "killed"]
    assert job_id not in runner.workspaces and key_record(job_id) is None and not killed  # killed, then removed


async def test_a_run_the_runner_killed_is_timed_out(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    runner.agent = ["killed"]
    assert await pipeline.advance(uuid.UUID(job_id)) == "timed_out"


async def test_6_the_gateway_budget_error_ends_the_job_as_budget_exceeded(
    make_user, client_for, forge_url, runner
) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    msg = "API Error: 400 Budget has been exceeded! Key=mwb-job-x Current cost: 10.1, Max budget: 10.0"
    lines = fake_runner.stream(fake_runner.tool("ls"), fake_runner.text(msg), fake_runner.result(ok=False, message=msg))
    runner.agent = [lambda ws, env: (1, lines)]
    assert await pipeline.advance(uuid.UUID(job_id)) == "budget_exceeded"
    job = await job_row(job_id)
    assert job.error_code == "budget_exceeded" and key_record(job_id) is None


async def test_a_lost_base_commit_fails_the_prepare(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).values(base_sha="0" * 40))
        await db.commit()
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    job = await job_row(job_id)
    assert (job.failed_stage, job.error_code) == ("preparing", "base_missing")


async def test_advance_is_idempotent(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    before = len(await events(job_id))
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"  # a second delivery does nothing
    assert len(await events(job_id)) == before


async def test_runner_client_maps_errors(runner, monkeypatch) -> None:
    from app.jobs.runner_client import RunnerError

    job = uuid.uuid4()
    with pytest.raises(RunnerError) as e:
        await runner_client.put_content(job, __import__("io").BytesIO(b""))
    assert e.value.code == "not_found" and e.value.status == 404
    assert await runner_client.run_status("0" * 16) is None
    await runner_client.kill_run("0" * 16)  # an unknown run is already gone
    await runner_client.create_workspace(job)
    assert await runner_client.read_mwb(job, "notes.md") is None
    runner_client.set_transport(None)
    monkeypatch.setattr(get_settings(), "runner_url", "http://127.0.0.1:9")
    with pytest.raises(RunnerError) as e:
        await runner_client.create_workspace(job)
    assert e.value.code == "unavailable"


# ---------- review actions and downloads (04 §5.5, §8; plan X5) ----------


async def reviewed(make_user, client_for, forge_url) -> tuple[dict, str]:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    return t, job_id


async def test_request_changes_starts_another_round_with_the_feedback(
    make_user, client_for, forge_url, runner, task_queue
) -> None:
    t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    seen = {}

    def agent(ws: Path, env: dict):
        seen["feedback"] = (ws / ".mwb" / "feedback.md").read_text()
        seen["prompt"] = (ws / ".mwb" / "prompt.md").read_text()
        return fake_runner.planned_change(ws, env)

    runner.agent = [agent]
    r = await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": "Use exponential backoff."})
    assert r.status_code == 200 and r.json()["status"] == "running" and r.json()["review_round"] == 1
    assert [j for j in task_queue.jobs.values() if j["task_name"] == "job_advance"]
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert "Use exponential backoff." in seen["feedback"] and "reviewer asked for changes" in seen["prompt"]
    job = await job_row(job_id)
    assert job.review_round == 1 and job.attempt == 1 and job.result["patch_artifact_id"]
    async with get_sessionmaker()() as db:
        names = {a.key.rsplit("/", 1)[1] for a in await db.scalars(select(Artifact).where(Artifact.job_id == job.id))}
    assert {"patch-r0-a1.diff", "patch-r1-a1.diff"} <= names
    assert (await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": ""})).status_code == 422
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).values(review_round=5))
        await db.commit()
    r = await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": "again"})
    assert r.status_code == 409 and "5 review rounds" in r.json()["detail"]


async def test_discard_removes_workspace_and_key(make_user, client_for, forge_url, runner) -> None:
    t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await c.post(f"/api/jobs/{job_id}/cancel")).status_code == 409  # resting: discard, not cancel
    r = await c.post(f"/api/jobs/{job_id}/discard")
    assert r.status_code == 200 and r.json()["status"] == "discarded"
    assert job_id not in runner.workspaces and key_record(job_id) is None
    async with get_sessionmaker()() as db:
        assert (await db.get(WorkItem, t["item"].id)).state == "approved"
    assert (await c.post(f"/api/jobs/{job_id}/discard")).status_code == 409


async def test_cancel_a_working_job(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    c = await client_for("alice", create=False)
    r = await c.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and key_record(job_id) is None
    assert await pipeline.advance(uuid.UUID(job_id)) == "cancelled"  # the queued task finds nothing to do


async def test_patch_and_artifacts_are_owner_only(make_user, client_for, forge_url, runner) -> None:
    t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    r = await c.get(f"/api/jobs/{job_id}/patch")
    assert r.status_code == 200 and "+# Retry transient failures (fake agent)" in r.text
    assert r.headers["content-type"].startswith("text/x-diff") and r.headers["x-content-type-options"] == "nosniff"
    log_id = (await job_row(job_id)).result["verify"][0]["log_artifact_id"]
    assert (await c.get(f"/api/artifacts/{log_id}")).status_code == 200
    bob = await client_for("bob")
    assert (await bob.get(f"/api/jobs/{job_id}/patch")).status_code == 404
    assert (await bob.get(f"/api/artifacts/{log_id}")).status_code == 404
    for action in ("cancel", "discard"):
        assert (await bob.post(f"/api/jobs/{job_id}/{action}")).status_code == 404


async def test_home_and_activity_show_jobs(make_user, client_for, forge_url, runner) -> None:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    c = await client_for("alice", create=False)
    [item] = (await c.get("/api/activity")).json()["items"]
    assert (item["kind"], item["title"], item["status"], item["step"]) == (
        "job",
        "Job for PAY-12",
        "queued",
        "Waiting to start",
    )
    assert (await c.get("/api/home")).json()["jobs_running"] == 1
    await pipeline.advance(uuid.UUID(job_id))
    home = (await c.get("/api/home")).json()
    assert home["jobs_running"] == 0 and home["board_needs"] == 1
    [card] = [n for n in home["needs_you"] if n["kind"] == "job_review"]
    assert card["href"] == f"/work/{t['item'].id}/job" and card["action"] == "Review changes"
