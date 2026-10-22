# ruff: noqa: F811 - pytest fixtures imported from test_job_pipeline
"""The push gate and orchestrator (design 04 §5.6, §6, §9 tests 7–8; plan X6).

The forge (tests/fakellm/forge.py) is a real Git server over HTTP that accepts pushes, a GitLab merge request API
and Jira comments, all in memory; `POST /fake/reset` empties it between tests.
"""

import subprocess
import uuid

import httpx2
import pytest
from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.enums import ApprovalSubject
from app.integrations.service import set_credential
from app.jobs import pipeline
from app.jobs import push as push_mod
from app.models import Approval, Artifact, AuditLog, InboxItem, Integration, Job, UserCredential, WorkItem
from tests import fake_runner
from tests.test_job_pipeline import (  # noqa: F401 - fixtures
    approved_ticket,
    forge_url,
    job_row,
    runner,
    start,
)

BRANCH = "mwb/PAY-12-retry-webhook-delivery"


@pytest.fixture
async def forge(forge_url):  # noqa: F811
    async with httpx2.AsyncClient(base_url=forge_url) as c:
        assert (await c.post("/fake/reset")).status_code == 200
        yield c


async def state(forge) -> dict:
    return (await forge.get("/fake/state")).json()


async def reviewed(make_user, client_for, forge_url, **kw) -> tuple[dict, str]:  # noqa: F811
    t = await approved_ticket(make_user, forge_url, jira_url=forge_url, **kw)
    job_id = await start(client_for, t)
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    return t, job_id


async def approve(c, job_id: str, edit=None) -> httpx2.Response:
    r = await c.get(f"/api/jobs/{job_id}/push-preview")
    assert r.status_code == 200, r.text
    preview = r.json()
    if edit:
        edit(preview)
    return await c.post(f"/api/jobs/{job_id}/approve-push", json=preview)


def git_show(forge_url: str, ref: str, path: str) -> str:  # noqa: F811
    from tests.fakellm.forge import git_root

    repo = git_root() / "team" / "payments.git"
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True).stdout


def commit_message(sha: str) -> str:
    from tests.fakellm.forge import git_root

    repo = git_root() / "team" / "payments.git"
    return subprocess.run(["git", "log", "-1", "--format=%an <%ae>%n%B", sha], cwd=repo, capture_output=True,
                          text=True).stdout  # fmt: skip


async def test_push_opens_one_merge_request_and_one_jira_comment(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,
    task_queue,  # noqa: F811
) -> None:
    t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    preview = (await c.get(f"/api/jobs/{job_id}/push-preview")).json()
    assert preview["target"] == {"host": "gitlab", "repo_url": f"{forge_url}/fake/git/team/payments.git",
                                 "branch": BRANCH, "base_branch": "main"}  # fmt: skip
    assert preview["commit"]["message"].endswith(f"MWB-Job: {job_id}")
    assert preview["merge_request"]["title"] == "Draft: PAY-12: Retry webhook delivery"
    assert preview["jira_comment"]["enabled"] and "<link placeholder>" in preview["jira_comment"]["body"]
    assert preview["acknowledgements"] == [] and preview["gerrit"] is None

    def edit(p):
        p["merge_request"]["title"] = "Draft: PAY-12: Retry webhooks (edited)"
        p["jira_comment"]["body"] = "Ready for review: <link placeholder>"

    r = await approve(c, job_id, edit)
    assert r.status_code == 200 and r.json()["status"] == "approved_push", r.text
    assert [j for j in task_queue.jobs.values() if j["task_name"] == "job_advance"]
    job = await job_row(job_id)
    async with get_sessionmaker()() as db:
        approval = await db.scalar(select(Approval).where(Approval.subject_type == ApprovalSubject.job_push))
    assert approval.payload_hash == push_mod.payload_hash(job.push["payload"]) and job.push["state"] == "approved"

    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    s = await state(forge)
    sha = s["refs"][f"refs/heads/{BRANCH}"]
    assert "Retry transient failures" in git_show(forge_url, sha, "app/webhooks.py")
    assert commit_message(sha).startswith("alice <a") and f"MWB-Job: {job_id}" in commit_message(sha)
    assert s["posts"] == {"mr": 1, "comment": 1}
    mr = s["mrs"][0]
    assert mr["title"] == "Draft: PAY-12: Retry webhooks (edited)" and mr["source_branch"] == BRANCH
    assert s["comments"]["PAY-12"][0]["body"] == f"Ready for review: {mr['web_url']}"
    job = await job_row(job_id)
    assert job.status == "pushed" and job.push["state"] == "done" and job.push["pushed_sha"] == sha
    assert job.push["mr_url"] == mr["web_url"] and job.push["jira_comment_id"]
    async with get_sessionmaker()() as db:
        assert (await db.get(WorkItem, job.work_item_id)).state == "in_review"
        actions = set(await db.scalars(select(AuditLog.action).where(AuditLog.target_id == job_id)))
        inbox = list(await db.scalars(select(InboxItem.title).where(InboxItem.owner_id == t["user"].id)))
    assert {"job.approve_push", "job.push", "job.mr", "job.jira_comment"} <= actions
    assert "PAY-12: changes pushed for review" in inbox
    assert job_id not in runner.workspaces  # workspace removed on pushed
    # a second delivery of the task does nothing
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    assert (await state(forge))["posts"] == {"mr": 1, "comment": 1}


async def test_changes_after_push_update_the_same_merge_request(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    first = (await state(forge))["refs"][f"refs/heads/{BRANCH}"]
    seen = {}

    def agent(ws, env):
        seen["file"] = (ws / "app" / "webhooks.py").read_text()
        seen["feedback"] = (ws / ".mwb" / "feedback.md").read_text()
        with (ws / "app" / "webhooks.py").open("a") as f:
            f.write("# with backoff\n")
        return 0, fake_runner.stream(fake_runner.tool("edit"), fake_runner.result())

    runner.agent = [agent]
    r = await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": "Add backoff."})
    assert r.status_code == 200 and r.json()["status"] == "preparing" and r.json()["review_round"] == 1, r.text
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert "Retry transient failures" in seen["file"] and "Add backoff." in seen["feedback"]  # built on the push
    job = await job_row(job_id)
    assert "resume" not in job.push and job.push["mr_url"]
    preview = (await c.get(f"/api/jobs/{job_id}/push-preview")).json()
    assert preview["jira_comment"] is None  # posted once already
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    s = await state(forge)
    second = s["refs"][f"refs/heads/{BRANCH}"]
    assert second != first and "# with backoff" in git_show(forge_url, second, "app/webhooks.py")
    assert s["posts"] == {"mr": 1, "comment": 1} and len(s["mrs"]) == 1


async def test_7_a_payload_edited_after_approval_is_refused(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    job = await job_row(job_id)
    tampered = {**job.push, "payload": {**job.push["payload"], "commit": {**job.push["payload"]["commit"],
                                                                         "message": "Something else"}}}  # fmt: skip
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job.id).values(push=tampered))
        await db.commit()
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    job = await job_row(job_id)
    assert job.error_code == "approval_mismatch" and job.failed_stage == "pushing"
    s = await state(forge)
    assert f"refs/heads/{BRANCH}" not in s["refs"] and s["posts"] == {"mr": 0, "comment": 0}


async def test_7_a_patch_changed_after_review_is_refused(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    job = await job_row(job_id)
    async with get_sessionmaker()() as db:
        art = await db.get(Artifact, uuid.UUID(job.result["patch_artifact_id"]))
        await db.commit()
    from app.jobs.artifacts import path_of

    path_of(art).write_bytes(path_of(art).read_bytes() + b"+AWS_KEY = 'x'\n")
    assert await pipeline.advance(uuid.UUID(job_id)) == "failed"
    assert (await job_row(job_id)).error_code == "approval_mismatch"


class Crash(BaseException):
    """Like the worker being killed: not an Exception, so nothing in the task handles it."""


@pytest.mark.parametrize("crash_at", [1, 2, 3], ids=["after-git-push", "after-merge-request", "after-jira-comment"])
async def test_8_a_push_redelivered_after_a_crash_creates_one_mr_and_one_comment(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,
    monkeypatch,
    crash_at,  # noqa: F811
) -> None:
    """The worker dies right after an external step, before its outcome is stored; the redelivered task repeats
    the step and must find what the first one did."""
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    real_save = push_mod._save
    calls = {"n": 0}

    async def save(job_id_, push):
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise Crash
        await real_save(job_id_, push)

    monkeypatch.setattr(push_mod, "_save", save)
    with pytest.raises(Crash):
        await pipeline.advance(uuid.UUID(job_id))
    assert (await job_row(job_id)).status == "pushing"
    monkeypatch.setattr(push_mod, "_save", real_save)
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    s = await state(forge)
    assert s["posts"] == {"mr": 1, "comment": 1}
    assert len(s["comments"]["PAY-12"]) == 1 and len(s["mrs"]) == 1
    assert (await job_row(job_id)).push["pushed_sha"] == s["refs"][f"refs/heads/{BRANCH}"]


async def test_failing_checks_need_the_acknowledgement(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    job = await job_row(job_id)
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job.id).values(result={**job.result, "failed_checks": True}))
        await db.commit()
    c = await client_for("alice", create=False)
    r = await approve(c, job_id)  # the preview leaves it unticked
    assert r.status_code == 409 and r.json()["field"] == "acknowledgements"
    assert "Push despite failing checks" in r.json()["detail"]
    r = await approve(c, job_id, lambda p: p.update(acknowledgements=[push_mod.PUSH_ACK]))  # ticked by the user
    assert r.status_code == 200 and (await job_row(job_id)).push["payload"]["acknowledgements"] == [push_mod.PUSH_ACK]


async def test_server_owned_fields_cant_be_edited_and_others_cant_push(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    r = await approve(c, job_id, lambda p: p["target"].update(branch="main"))
    assert r.status_code == 409 and r.json()["code"] == "plan_changed"
    r = await approve(c, job_id, lambda p: p["commit"].update(author_email="ceo@example.com"))
    assert r.status_code == 422
    r = await approve(c, job_id, lambda p: p.update(patch_sha256="0" * 64))
    assert r.status_code == 409
    bob = await client_for("bob")
    assert (await bob.get(f"/api/jobs/{job_id}/push-preview")).status_code == 404
    assert (await bob.post(f"/api/jobs/{job_id}/approve-push", json={})).status_code in (404, 422)
    preview = (await c.get(f"/api/jobs/{job_id}/push-preview")).json()
    assert (await c.post(f"/api/jobs/{job_id}/approve-push", json=preview)).status_code == 200
    r = await c.post(f"/api/jobs/{job_id}/approve-push", json=preview)
    assert r.status_code == 409  # approved already: no longer waiting for review
    assert (await c.get(f"/api/jobs/{job_id}/push-preview")).status_code == 409


async def test_gerrit_upload_keeps_its_change_id_across_rounds(
    make_user,
    client_for,
    forge_url,
    forge,
    runner,  # noqa: F811
) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url, host="gerrit", token="gerrit-pass")
    c = await client_for("alice", create=False)
    cid = push_mod.change_id(uuid.UUID(job_id))
    preview = (await c.get(f"/api/jobs/{job_id}/push-preview")).json()
    assert preview["merge_request"] is None and preview["gerrit"] == {"topic": "mwb-PAY-12"}
    assert f"Change-Id: {cid}" in preview["commit"]["message"].splitlines()
    msg = "\n".join(line for line in preview["commit"]["message"].splitlines() if not line.startswith("Change-Id"))
    r = await approve(c, job_id, lambda p: p["commit"].update(message=msg))
    assert r.status_code == 422 and "Change-Id" in r.json()["detail"]
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    job = await job_row(job_id)
    assert job.push["change_url"] == f"{forge_url}/c/team/payments/+/1"
    runner.agent = []
    assert (await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": "Again."})).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    changes = {k: v for k, v in (await state(forge))["refs"].items() if k.startswith("refs/changes/")}
    assert len(changes) == 2  # two patch sets
    assert all(f"Change-Id: {cid}" in commit_message(sha) for sha in changes.values())
    assert (await job_row(job_id)).push["change_url"] == f"{forge_url}/c/team/payments/+/1"


async def test_a_rejected_push_marks_the_credential_invalid_and_can_be_retried(
    make_user, client_for, forge_url, forge, runner, task_queue
) -> None:
    t, job_id = await reviewed(make_user, client_for, forge_url, token="glpat-revoked")
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"  # 08 §F5: back to review, not failed
    job = await job_row(job_id)
    assert job.push["error"]["code"] == "credential_invalid" and "glpat-revoked" not in job.push["error"]["message"]
    async with get_sessionmaker()() as db:
        q = select(UserCredential.status).where(
            UserCredential.user_id == t["user"].id, UserCredential.integration_id == t["project"].integration_id
        )
        statuses = set(await db.scalars(q))
        titles = list(await db.scalars(select(InboxItem.title).where(InboxItem.owner_id == t["user"].id)))
    assert statuses == {"invalid"} and "Your Forge credential was rejected" in titles
    assert "PAY-12: the push failed" in titles
    # The user fixes the token, then retries with the approval already given.
    async with get_sessionmaker()() as db:
        integration = await db.get(Integration, t["project"].integration_id)
        await set_credential(db, t["user"].id, integration, "token", "glpat-e2e-token")
        await db.commit()
    r = await c.post(f"/api/jobs/{job_id}/retry-push")
    assert r.status_code == 200 and r.json()["status"] == "approved_push", r.text
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    assert "error" not in (await job_row(job_id)).push
    assert (await state(forge))["posts"] == {"mr": 1, "comment": 1}


async def test_a_push_that_fails_midway_resumes_on_retry(
    make_user, client_for, forge_url, forge, runner, monkeypatch
) -> None:
    from app.connectors import base

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(base, "sleep", no_wait)
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    await forge.post("/fake/fail/mr_create", params={"times": 3})  # every attempt of the connector's retries
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    job = await job_row(job_id)
    assert job.push["state"] == "branch_pushed" and job.push["error"]["code"] == "review_request_failed"
    assert job_id in runner.workspaces  # kept for the retry
    sha = (await state(forge))["refs"][f"refs/heads/{BRANCH}"]
    # Retry refuses a payload that no longer matches its approval.
    async with get_sessionmaker()() as db:
        tampered = {**job.push, "payload": {**job.push["payload"], "patch_sha256": "0" * 64}}
        await db.execute(update(Job).where(Job.id == job.id).values(push=tampered))
        await db.commit()
    assert (await c.post(f"/api/jobs/{job_id}/retry-push")).status_code == 409
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job.id).values(push=job.push))
        await db.commit()
    r = await c.post(f"/api/jobs/{job_id}/retry-push")
    assert r.status_code == 200, r.text
    assert (await c.post(f"/api/jobs/{job_id}/retry-push")).status_code == 409  # not waiting for review any more
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    s = await state(forge)
    assert s["posts"] == {"mr": 1, "comment": 1} and s["refs"][f"refs/heads/{BRANCH}"] == sha  # resumed, not redone


async def test_retry_push_needs_a_failed_push(make_user, client_for, forge_url, forge, runner) -> None:
    _t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    r = await c.post(f"/api/jobs/{job_id}/retry-push")
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    bob = await client_for("bob")
    assert (await bob.post(f"/api/jobs/{job_id}/retry-push")).status_code == 404
