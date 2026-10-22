"""The push gate and the orchestrator (design 04 §5.6, §6; rule R5).

`preview()` builds the exact outbound content; the user may edit the texts and approves it with `approve()`,
which binds `sha256(canonical JSON)` to an `approvals` row and stores the payload in `jobs.push.payload`.
`push()` runs in the job's task (queue `jobs`, `lock=job:<id>`) with no sandbox: it re-checks the approval and
the patch, commits, pushes, opens or updates the merge request (GitLab) or reads the change (Gerrit), comments
on Jira once, and finishes. Each step records its outcome in `jobs.push` before the next one starts, so a task
redelivered after a crash continues where it stopped and creates nothing twice:

- the commit is deterministic (author and committer dates are the approval's time), so a re-push of the same
  round produces the same sha, and a lease that fails because the branch already holds it counts as done;
- the merge request is looked up by source branch before one is created;
- the Jira comment is looked up by its exact text before one is posted.
"""

import asyncio
import hashlib
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

from fastapi import status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit
from app.connectors import jira as jira_api
from app.connectors.base import Connection, ConnectorError, call_json, mark_rejected
from app.db import get_sessionmaker
from app.enums import ApprovalDecision, ApprovalSubject, JobEventType, JobStatus
from app.errors import AppError
from app.inbox.service import add_item
from app.integrations.service import get_credentials
from app.jobs import artifacts
from app.jobs.state import record_event, transition, transition_in
from app.knowledge.ingest.repos import auth_header, ca_file_for, git_env, project_integration
from app.logs import log
from app.models import Approval, Artifact, Integration, JiraIssue, Job, PlanRevision, Project, User, WorkItem
from app.plans import approvals
from app.plans.schema import canonical_json

S = JobStatus
PUSH_ACK = "push despite failing checks"
LINK_PLACEHOLDER = "<link placeholder>"
GIT_TIMEOUT_S = 600
CHANGE_URL = re.compile(r"(https?://\S+?/(?:c/\S+?/\+/|#/c/)\d+)")


class StepFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


# ---------- the payload (04 §5.6) ----------


class Target(BaseModel):
    host: Literal["gitlab", "gerrit"]
    repo_url: str
    branch: str
    base_branch: str


class CommitIn(BaseModel):
    author_name: str
    author_email: str
    message: str = Field(min_length=1, max_length=20_000)


class MergeRequestIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=50_000)


class GerritIn(BaseModel):
    topic: str


class JiraCommentIn(BaseModel):
    enabled: bool
    body: str = Field(max_length=20_000)


class PushPayload(BaseModel):
    target: Target
    commit: CommitIn
    merge_request: MergeRequestIn | None = None
    gerrit: GerritIn | None = None
    jira_comment: JiraCommentIn | None = None
    patch_sha256: str
    acknowledgements: list[str] = []


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def change_id(job_id: uuid.UUID) -> str:
    """Gerrit's Change-Id, stable for the job: later review rounds become new patch sets of the same change."""
    return "I" + hashlib.sha1(f"mwb-job:{job_id}".encode()).hexdigest()  # noqa: S324 - an id, not security


def _conflict(message: str, field: str | None = None) -> AppError:
    return AppError(status.HTTP_409_CONFLICT, "conflict", message, **({"field": field} if field else {}))


async def _context(db: AsyncSession, job: Job) -> dict[str, Any]:
    rev = await db.get(PlanRevision, job.plan_revision_id)
    item = await db.get(WorkItem, job.work_item_id)
    issue = await db.get(JiraIssue, item.jira_issue_id) if item else None
    project = await db.get(Project, job.project_id) if job.project_id else None
    if project is None:
        raise _conflict("The ticket's repository was removed.")
    user = await db.get(User, job.owner_id)
    jira = await db.get(Integration, issue.integration_id) if issue else None
    jira_ok = False
    if jira is not None and jira.enabled:
        try:
            jira_ok = bool(await get_credentials(db, job.owner_id, jira))
        except Exception:  # noqa: BLE001 - unreadable: no comment offered
            jira_ok = False
    return {"rev": rev, "item": item, "issue": issue, "project": project, "user": user, "jira": jira,
            "jira_ok": jira_ok}  # fmt: skip


def _jira_url(jira: Integration | None, key: str) -> str | None:
    return f"{jira.base_url.rstrip('/')}/browse/{key}" if jira else None


def build_preview(job: Job, c: dict[str, Any]) -> dict[str, Any]:
    """The default payload. Server-owned fields (target, author, patch) are re-derived on approval."""
    rev, project, user, issue = c["rev"], c["project"], c["user"], c["issue"]
    doc = rev.content if rev else {}
    key = issue.key if issue else "JOB"
    result = job.result or {}
    previous = (job.push or {}).get("payload") or {}
    gerrit = project.host == "gerrit"
    message = (doc.get("commit_message") or f"{key}: {doc.get('title', 'Change')}").strip()
    message += f"\n\nMWB-Job: {job.id}"
    if gerrit:
        message += f"\nChange-Id: {change_id(job.id)}"
    files = result.get("files") or []
    checks = result.get("checks") or []
    warn = [ch["name"] for ch in checks if ch.get("status") != "pass"]
    lines = [
        doc.get("summary", "").strip(),
        "",
        f"Jira: {_jira_url(c['jira'], key) or key}",
        f"Files changed: {len(files)}. Checks: {'all passed' if not warn else 'see ' + ', '.join(warn)}.",
        "",
        f"Prepared by MyWorkBuddy (job {job.id}) from an approved plan; reviewed by {user.username} before pushing.",
    ]
    description = "\n".join(lines).strip()
    link_word = "Change uploaded" if gerrit else "Merge request opened"
    comment = f"{link_word}: {LINK_PLACEHOLDER}\n\n{doc.get('title', '').strip()}"
    already_commented = bool((job.push or {}).get("jira_comment_id"))
    prev_mr = previous.get("merge_request") or {}
    return {
        "target": {"host": project.host, "repo_url": project.repo_url, "branch": job.branch_name,
                   "base_branch": job.base_branch},
        "commit": {
            "author_name": user.display_name or user.username,
            "author_email": user.email or f"{user.username}@users.noreply.invalid",
            "message": message,
        },
        "merge_request": None if gerrit else {
            "title": prev_mr.get("title") or f"Draft: {key}: {doc.get('title', 'Change')}"[:255],
            "description": prev_mr.get("description") or description,
        },
        "gerrit": {"topic": f"mwb-{key}"} if gerrit else None,
        "jira_comment": None if already_commented or not c["jira_ok"] else {"enabled": True, "body": comment},
        "patch_sha256": result.get("patch_sha256", ""),
        "acknowledgements": [],  # the user ticks "push despite failing checks" themselves (07 §5.9 step 6)
    }  # fmt: skip


async def preview(db: AsyncSession, job: Job) -> dict[str, Any]:
    if job.status != S.awaiting_review or not job.result:
        raise _conflict("A push can be reviewed once the job is waiting for review.")
    return build_preview(job, await _context(db, job))


async def approve(db: AsyncSession, job: Job, body: PushPayload, user: User, ip: str | None) -> Approval:
    """Check the edited payload against what the server owns, bind it to an approval, queue the push."""
    if job.status != S.awaiting_review or not job.result:
        raise _conflict("Only a job waiting for review can be pushed.")
    expected = build_preview(job, await _context(db, job))
    payload = body.model_dump(mode="json")
    for fixed in ("target", "patch_sha256"):
        if payload[fixed] != expected[fixed]:
            raise AppError(status.HTTP_409_CONFLICT, "plan_changed", "The job changed meanwhile. Reload the preview.")
    for name in ("author_name", "author_email"):
        if payload["commit"][name] != expected["commit"][name]:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "The author can't be changed.")
    if (payload["merge_request"] is None) != (expected["merge_request"] is None) or (
        (payload["gerrit"] is None) != (expected["gerrit"] is None)
    ):
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "The review request doesn't fit the host."
        )
    if payload["gerrit"] and payload["gerrit"] != expected["gerrit"]:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "The Gerrit topic can't be changed.")
    if expected["jira_comment"] is None and payload["jira_comment"] is not None:
        payload["jira_comment"] = None  # already posted, or no Jira access: nothing to approve
    cid = change_id(job.id)
    if payload["gerrit"] and f"Change-Id: {cid}" not in payload["commit"]["message"].splitlines():
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
            f"Keep the line 'Change-Id: {cid}' in the commit message.", field="commit.message",
        )  # fmt: skip
    if (job.result or {}).get("failed_checks") and PUSH_ACK not in payload["acknowledgements"]:
        raise AppError(
            status.HTTP_409_CONFLICT, "conflict", "Some checks failed: tick “Push despite failing checks”.",
            field="acknowledgements",
        )  # fmt: skip
    payload["acknowledgements"] = sorted(set(payload["acknowledgements"]) & {PUSH_ACK})
    digest = payload_hash(payload)
    row = await approvals.record(
        db, owner_id=job.owner_id, subject_type=ApprovalSubject.job_push, subject_id=job.id, payload_hash=digest,
        decision=ApprovalDecision.approved, acknowledged=payload["acknowledgements"], ip=ip,
    )  # fmt: skip
    push = {k: v for k, v in (job.push or {}).items() if k != "error"}
    push.update(payload=payload, approval_id=str(row.id), round=job.review_round, state="approved",
                approved_at=row.decided_at.isoformat() if row.decided_at else None)  # fmt: skip
    moved = await transition_in(
        db, job.id, S.awaiting_review, S.approved_push, actor_id=user.id, ip=ip, push=push,
        detail={"note": "Push approved.", "approval_id": str(row.id)},
    )  # fmt: skip
    if moved is None:
        raise AppError(status.HTTP_409_CONFLICT, "plan_changed", "The job changed meanwhile. Reload.")
    await audit(db, "job.approve_push", user_id=user.id, target_type="job", target_id=str(job.id),
                detail={"approval_id": str(row.id), "payload_hash": digest}, ip=ip)  # fmt: skip
    return row


# ---------- the orchestrator (04 §6) ----------


async def _save(job_id: uuid.UUID, push: dict[str, Any]) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(push=push))
        await db.commit()


async def _audit(job: Job, action: str, detail: dict[str, Any]) -> None:
    async with get_sessionmaker()() as db:
        await audit(db, action, user_id=job.owner_id, target_type="job", target_id=str(job.id), detail=detail)
        await db.commit()


async def _verified(job: Job) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Step 1: the stored payload is the approved one, and the patch is the reviewed one. Reads, then closes."""
    push = dict(job.push or {})
    payload = push.get("payload")
    async with get_sessionmaker()() as db:
        latest = await approvals.latest(db, ApprovalSubject.job_push, job.id)
        art_id = (job.result or {}).get("patch_artifact_id")
        art = await db.get(Artifact, uuid.UUID(art_id)) if art_id else None
        c = await _context(db, job)
        project, integration = c["project"], await project_integration(db, c["project"])
        header = await auth_header(db, project, job.owner_id)
        creds = await get_credentials(db, job.owner_id, integration) if integration else {}
        jira_creds = await get_credentials(db, job.owner_id, c["jira"]) if c["jira"] and c["jira_ok"] else {}
        await db.commit()
    if (
        payload is None
        or latest is None
        or latest.decision != ApprovalDecision.approved
        or str(latest.id) != push.get("approval_id")
        or latest.payload_hash != payload_hash(payload)
    ):
        raise StepFailed("approval_mismatch", "The push content doesn't match what was approved.")
    data = artifacts.path_of(art).read_bytes() if art is not None and artifacts.path_of(art).is_file() else None
    if (
        data is None
        or hashlib.sha256(data).hexdigest() != payload.get("patch_sha256")
        or art.sha256 != payload.get("patch_sha256")
    ):
        raise StepFailed("approval_mismatch", "The patch isn't the one that was reviewed.")
    env = {"project": project, "integration": integration, "header": header, "creds": creds, "jira": c["jira"],
           "jira_creds": jira_creds, "issue": c["issue"],
           "approved_at": push.get("approved_at") or latest.decided_at.isoformat()}  # fmt: skip
    return payload, data, env


async def _git(
    *args: str, cwd: Path, header: str | None, ca: Path | None, extra: dict[str, str] | None = None
) -> tuple[int, str, str]:
    env = git_env(header, ca) | (extra or {})
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise StepFailed("push_failed", f"git {args[0]} timed out.") from None
    text = err.decode(errors="replace")
    if header:
        text = text.replace(header, "***").replace(header.rsplit(" ", 1)[-1], "***")
    return proc.returncode or 0, out.decode(errors="replace"), text


def _auth_refused(stderr: str) -> bool:
    low = stderr.lower()
    signs = ("401", "403", "authentication failed", "access denied", "permission denied", "could not read username",
             "terminal prompts disabled")  # fmt: skip
    return any(s in low for s in signs)


async def _refused(job: Job, env: dict[str, Any]) -> StepFailed:
    if env["integration"] is not None:
        await mark_rejected(Connection(job.owner_id, env["integration"], {}), 401)
    return StepFailed("credential_invalid", f"{env['project'].host.capitalize()} rejected your credential.")


async def _commit_and_push(job: Job, payload: dict[str, Any], patch: bytes, env: dict[str, Any]) -> str:
    """Steps 2–3: a fresh clone at base_sha, the reviewed patch, one commit, one push. Returns the pushed sha."""
    push = job.push or {}
    project = env["project"]
    header, ca = env["header"], ca_file_for(env["integration"])
    work = Path(tempfile.mkdtemp(prefix=f"push-{job.id}-", dir=_tmp_root()))
    try:
        repo = work / "repo"
        code, _, err = await _git("clone", "-q", "--no-tags", "--single-branch", "--branch", job.base_branch, "--",
                                  project.repo_url, str(repo), cwd=work, header=header, ca=ca)  # fmt: skip
        if code:
            raise (
                await _refused(job, env)
                if _auth_refused(err)
                else StepFailed("push_failed", f"Clone failed: {err[-300:]}")
            )
        await _git("config", "core.hooksPath", "/dev/null", cwd=repo, header=None, ca=None)
        code, _, err = await _git("checkout", "-q", "-B", "mwb-push", job.base_sha, cwd=repo, header=None, ca=None)
        if code:
            raise StepFailed("base_missing", f"The base commit {job.base_sha[:12]} is gone.")
        (work / "change.diff").write_bytes(patch)
        if patch.strip():
            code, _, err = await _git("apply", "--index", "--whitespace=nowarn", str(work / "change.diff"), cwd=repo,
                                      header=None, ca=None)  # fmt: skip
            if code:
                raise StepFailed("patch_failed", f"The patch doesn't apply: {err[-300:]}")
        (work / "message.txt").write_text(payload["commit"]["message"])
        who = payload["commit"]
        when = env["approved_at"]
        identity = {
            "GIT_AUTHOR_NAME": who["author_name"], "GIT_AUTHOR_EMAIL": who["author_email"], "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_NAME": who["author_name"], "GIT_COMMITTER_EMAIL": who["author_email"],
            "GIT_COMMITTER_DATE": when,
        }  # fmt: skip
        code, _, err = await _git("commit", "-q", "--no-verify", "-F", str(work / "message.txt"), cwd=repo,
                                  header=None, ca=None, extra=identity)  # fmt: skip
        if code:
            raise StepFailed("push_failed", f"Commit failed: {err[-300:]}")
        _, sha, _ = await _git("rev-parse", "HEAD", cwd=repo, header=None, ca=None)
        sha = sha.strip()
        if project.host == "gerrit":
            topic = (payload.get("gerrit") or {}).get("topic", "")
            code, _, err = await _git("push", "origin", f"HEAD:refs/for/{job.base_branch}%topic={topic}", cwd=repo,
                                      header=header, ca=ca)  # fmt: skip
            if code and "no new changes" not in err:
                raise (
                    await _refused(job, env)
                    if _auth_refused(err)
                    else StepFailed("push_failed", f"Push failed: {err[-300:]}")
                )
            if m := CHANGE_URL.search(err):
                push = {**push, "change_url": m.group(1)}
        else:
            ref = f"refs/heads/{job.branch_name}"
            lease = f"--force-with-lease={ref}:{push.get('pushed_sha') or ''}"
            code, _, err = await _git("push", lease, "origin", f"HEAD:{ref}", cwd=repo, header=header, ca=ca)
            if code:
                if _auth_refused(err):
                    raise await _refused(job, env)
                # Already there from an earlier delivery of this task: the same deterministic commit.
                _, remote, _ = await _git("ls-remote", "origin", ref, cwd=repo, header=header, ca=ca)
                if remote.split("\t", 1)[0].strip() != sha:
                    raise StepFailed(
                        "branch_moved", f"The branch {job.branch_name} changed on the server since the last push."
                    )
        job.push = {**push, "pushed_sha": sha, "state": "branch_pushed"}
        return sha
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _tmp_root() -> Path:
    from app.jobs.pipeline import _tmp_root as root

    return root()


def _gitlab_project(project: Project, integration: Integration) -> str:
    path = urlparse(project.repo_url).path
    prefix = urlparse(integration.base_url).path.rstrip("/")
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix) :]
    return quote(path.strip("/").removesuffix(".git"), safe="")


async def _merge_request(job: Job, payload: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    """Step 4 (GitLab): find the MR by source branch; create it if none, else update its title and description."""
    conn = Connection(job.owner_id, env["integration"], env["creds"])
    pid = _gitlab_project(env["project"], env["integration"])
    mr_in = payload["merge_request"]
    try:
        found = await call_json(conn, "GET", f"/api/v4/projects/{pid}/merge_requests",
                                params={"source_branch": job.branch_name, "state": "opened"}) or []  # fmt: skip
        if found:
            mr = await call_json(conn, "PUT", f"/api/v4/projects/{pid}/merge_requests/{found[0]['iid']}",
                                 json={"title": mr_in["title"], "description": mr_in["description"]})  # fmt: skip
            action = "updated"
        else:
            mr = await call_json(conn, "POST", f"/api/v4/projects/{pid}/merge_requests", json={
                "source_branch": job.branch_name, "target_branch": job.base_branch, "title": mr_in["title"],
                "description": mr_in["description"], "remove_source_branch": True,
            })  # fmt: skip
            action = "created"
    except ConnectorError as exc:
        raise StepFailed(
            "credential_invalid" if exc.code == "auth_failed" else "review_request_failed", exc.message
        ) from None
    finally:
        await conn.close()
    return {"mr_iid": mr.get("iid"), "mr_url": mr.get("web_url"), "action": action}


async def _gerrit_change(job: Job, env: dict[str, Any]) -> dict[str, Any]:
    """Step 4 (Gerrit): the change URL from the push output, else the change found by its Change-Id."""
    push = job.push or {}
    if push.get("change_url"):
        return {"change_url": push["change_url"]}
    conn = Connection(job.owner_id, env["integration"], env["creds"])
    try:
        changes = await call_json(conn, "GET", "/a/changes/", params={"q": f"change:{change_id(job.id)}"}) or []
    except ConnectorError as exc:
        raise StepFailed(
            "credential_invalid" if exc.code == "auth_failed" else "review_request_failed", exc.message
        ) from None
    finally:
        await conn.close()
    if not changes:
        raise StepFailed("review_request_failed", "Gerrit has no change for this push.")
    ch = changes[0]
    base = env["integration"].base_url.rstrip("/")
    return {"change_url": f"{base}/c/{ch.get('project')}/+/{ch.get('_number')}", "change_number": ch.get("_number")}


async def _jira_comment(job: Job, payload: dict[str, Any], env: dict[str, Any], link: str) -> dict[str, Any] | None:
    """Step 5: the approved text with the link filled in, posted once (an identical comment counts as posted)."""
    comment = payload.get("jira_comment") or {}
    if not comment.get("enabled") or (job.push or {}).get("jira_comment_id") or env["issue"] is None:
        return None
    body = comment["body"].replace(LINK_PLACEHOLDER, link)
    conn = Connection(job.owner_id, env["jira"], env["jira_creds"])
    key = env["issue"].key
    api = f"/rest/api/{conn.integration.settings.get('api_version', '2')}/issue/{quote(key)}/comment"
    try:
        existing = await call_json(conn, "GET", api, params={"maxResults": 100}) or {}
        for c in existing.get("comments", []):
            if jira_api.adf_text(c.get("body")).strip() == body.strip():
                posted = c
                break
        else:
            v3 = conn.integration.settings.get("api_version") == "3"
            doc: Any = body
            if v3:
                doc = {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in body.split("\n\n") if p
                ]}  # fmt: skip
            posted = await call_json(conn, "POST", api, json={"body": doc})
    except ConnectorError as exc:
        raise StepFailed(
            "credential_invalid" if exc.code == "auth_failed" else "jira_comment_failed", exc.message
        ) from None
    finally:
        await conn.close()
    cid = str(posted.get("id"))
    return {
        "jira_comment_id": cid,
        "jira_comment_url": f"{env['jira'].base_url.rstrip('/')}/browse/{key}?focusedCommentId={cid}",
    }


async def push(job: Job, key: str) -> None:
    """The steps of 04 §6, each recorded in `jobs.push` so a redelivered task resumes after the last one done."""
    if job.status == S.approved_push:
        if await transition(job.id, S.approved_push, S.pushing, detail={"note": "Pushing."}) is None:
            return
    payload, patch, env = await _verified(job)
    state = (job.push or {}).get("state")
    if state == "approved":
        sha = await _commit_and_push(job, payload, patch, env)
        await _save(job.id, job.push)
        await _audit(job, "job.push", {"approval_id": job.push.get("approval_id"), "sha": sha,
                                       "branch": job.branch_name, "host": env["project"].host})  # fmt: skip
        await record_event(job.id, JobEventType.push, {"note": f"Pushed {sha[:12]} to {job.branch_name}."})
        state = "branch_pushed"
    if state == "branch_pushed":
        if env["project"].host == "gerrit":
            found = await _gerrit_change(job, env)
            link = found["change_url"]
        else:
            found = await _merge_request(job, payload, env)
            link = found["mr_url"]
        job.push = {**job.push, **{k: v for k, v in found.items() if k != "action"}, "state": "review_requested"}
        await _save(job.id, job.push)
        await _audit(job, "job.mr", {"approval_id": job.push.get("approval_id"), "url": link,
                                     "action": found.get("action", "found")})  # fmt: skip
        await record_event(job.id, JobEventType.push, {"note": f"Review request: {link}", "url": link})
        state = "review_requested"
    if state == "review_requested":
        link = job.push.get("mr_url") or job.push.get("change_url") or ""
        posted = await _jira_comment(job, payload, env, link)
        if posted:
            job.push = {**job.push, **posted}
            await _audit(job, "job.jira_comment", {"approval_id": job.push.get("approval_id"),
                                                   "comment_id": posted["jira_comment_id"]})  # fmt: skip
            await record_event(
                job.id, JobEventType.push, {"note": "Commented on Jira.", "url": posted["jira_comment_url"]}
            )
        job.push = {**job.push, "state": "done"}
        await _save(job.id, job.push)
    async with get_sessionmaker()() as db:
        moved = await transition_in(db, job.id, S.pushing, S.pushed, current_run=None,
                                    detail={"note": "Pushed; watching for merge."})  # fmt: skip
        if moved is not None:
            link = job.push.get("mr_url") or job.push.get("change_url")
            add_item(db, job.owner_id, kind="job", severity="info", title=f"{key}: changes pushed for review",
                     body_md=f"[Open the review request]({link})" if link else "Pushed.",
                     link=f"/work/{job.work_item_id}/job")  # fmt: skip
        await db.commit()
    from app.jobs.pipeline import cleanup

    await cleanup(job.id)
    log.info("job.pushed", job_id=str(job.id))


# ---------- a failed push goes back to review (08 §F5) ----------

END_CODES = {"approval_mismatch"}  # the approval or the patch is no longer what was reviewed: the job ends


async def back_to_review(job: Job, key: str, code: str, message: str) -> bool:
    """`pushing → awaiting_review`, keeping how far the push got, the approval, the workspace and the key."""
    async with get_sessionmaker()() as db:
        fresh = await db.scalar(select(Job).where(Job.id == job.id).with_for_update())
        if fresh is None:
            return False
        push = {**(fresh.push or {}), "error": {"code": code, "message": message[:1000],
                                                "at": datetime.now(UTC).isoformat()}}  # fmt: skip
        moved = await transition_in(
            db, job.id, S.pushing, S.awaiting_review, push=push, current_run=None,
            detail={"note": f"Push failed: {message[:300]}", "code": code},
        )  # fmt: skip
        if moved is not None:
            add_item(db, job.owner_id, kind="job", severity="warning", title=f"{key}: the push failed",
                     body_md=f"{message[:500]}\n\nRetry it from the Job tab.",
                     link=f"/work/{job.work_item_id}/job")  # fmt: skip
        await db.commit()
    log.warning("job.push_failed", job_id=str(job.id), code=code)
    return moved is not None


async def retry(db: AsyncSession, job: Job, user: User, ip: str | None) -> None:
    """Push again with the approval already given, if it still binds the stored payload and the reviewed patch."""
    push = job.push or {}
    if job.status != S.awaiting_review or not push.get("error") or not push.get("payload"):
        raise _conflict("Only a job whose push failed can retry it.")
    latest = await approvals.latest(db, ApprovalSubject.job_push, job.id)
    if (
        latest is None
        or latest.decision != ApprovalDecision.approved
        or str(latest.id) != push.get("approval_id")
        or latest.payload_hash != payload_hash(push["payload"])
        or (job.result or {}).get("patch_sha256") != push["payload"].get("patch_sha256")
    ):
        raise AppError(
            status.HTTP_409_CONFLICT, "plan_changed", "The push changed since it was approved. Review it again."
        )
    moved = await transition_in(
        db, job.id, S.awaiting_review, S.approved_push, actor_id=user.id, ip=ip,
        push={k: v for k, v in push.items() if k != "error"},
        detail={"note": "Retrying the push.", "approval_id": str(latest.id)},
    )  # fmt: skip
    if moved is None:
        raise AppError(status.HTTP_409_CONFLICT, "plan_changed", "The job changed meanwhile. Reload.")
    await audit(db, "job.retry_push", user_id=user.id, target_type="job", target_id=str(job.id),
                detail={"approval_id": str(latest.id), "after": push.get("state")}, ip=ip)  # fmt: skip
