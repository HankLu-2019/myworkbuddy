"""The job pipeline (design 04 §5.2–5.4): prepare → run agent → verify, with retries, in one idempotent task.

`advance(job_id)` (task `job_advance`, queue `jobs`, `lock=job:<id>`) looks at the job's status and does the next
step until the job rests (awaiting review) or ends. Every step can be re-run after a crash: a prepare starts the
workspace over, a run the job was waiting on (`jobs.current_run`) is followed again rather than started twice.
Network calls (git, the runner, the gateway) run with no DB transaction open (R1).
"""

import asyncio
import io
import json
import shutil
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import update

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import JobEventType, JobStatus
from app.inbox.email import app_url, queue_email
from app.inbox.service import add_item
from app.jobs import artifacts, keys, runner_client
from app.jobs import push as push_mod
from app.jobs.checks import parse_patch, run_checks
from app.jobs.runner_client import RunnerError
from app.jobs.state import record_event, transition, transition_in
from app.jobs.task_files import (
    AgentOutcome,
    feedback_md,
    parse_line,
    prompt_md,
    review_feedback_md,
    task_md,
    verify_json,
)
from app.knowledge.ingest.repos import auth_header, ca_file_for, project_integration, run_git
from app.logs import log
from app.models import Artifact, JiraIssue, Job, PlanRevision, Project, User, WorkItem
from app.plans import conventions

S = JobStatus
EVENT_INTERVAL_S = 1.0  # at most one event of each kind per second (04 §5.3)
LOG_GRACE_S = 90  # past timeout_s, the worker stops waiting and kills the run itself
NOTES_CHARS = 10_000


class StepFailed(Exception):
    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage, self.code, self.message = stage, code, message


async def defer_advance(job_id: uuid.UUID) -> bool:
    from app.tasks.app import defer_once, job_advance_task

    return await defer_once(job_advance_task, f"job:{job_id}", lock=f"job:{job_id}", job_id=str(job_id))


async def _load(job_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_sessionmaker()() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return None
        rev = await db.get(PlanRevision, job.plan_revision_id)
        item = await db.get(WorkItem, job.work_item_id)
        issue = await db.get(JiraIssue, item.jira_issue_id) if item else None
        user = await db.get(User, job.owner_id)
        await db.commit()
        project = await db.get(Project, job.project_id) if job.project_id else None
        return {"job": job, "doc": rev.content if rev else {}, "key": issue.key if issue else "JOB", "user": user,
                "conventions_md": project.conventions_md if project else None}  # fmt: skip


async def _set(job_id: uuid.UUID, **values: Any) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(**values))
        await db.commit()


# ---------- prepare (04 §5.2) ----------


def _tar(directory: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for path in sorted(directory.rglob("*")):
            t.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    buf.seek(0)
    return buf


def overlay(files: dict[str, str]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
            t.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


async def _clone(job: Job, work: Path) -> None:
    """Clone the base branch with the job owner's token (GIT_CONFIG_* env, R7), check out base_sha, sanitise."""
    async with get_sessionmaker()() as db:
        project = await db.get(Project, job.project_id) if job.project_id else None
        if project is None:
            raise StepFailed("preparing", "project_missing", "The ticket's repository was removed.")
        header = await auth_header(db, project, job.owner_id)
        ca = ca_file_for(await project_integration(db, project))
        url = project.repo_url
        await db.commit()
    repo = work / "repo"
    try:
        await run_git("clone", "-q", "--no-tags", "--single-branch", "--branch", job.base_branch, "--", url, str(repo),
                      cwd=work, auth_header=header, ca_file=ca, timeout=900)  # fmt: skip
    except RuntimeError as exc:
        raise StepFailed("preparing", "clone_failed", f"The repository couldn't be cloned: {str(exc)[:300]}") from None
    try:
        await run_git("checkout", "-q", "--detach", job.base_sha, cwd=repo)
    except RuntimeError:
        raise StepFailed("preparing", "base_missing", f"The plan's base commit {job.base_sha[:12]} is gone.") from None
    # Sanitise (04 §5.2): no remote, no hooks, no credentials anywhere in the clone.
    await run_git("remote", "remove", "origin", cwd=repo)
    hooks = repo / ".git" / "hooks"
    shutil.rmtree(hooks, ignore_errors=True)
    hooks.mkdir()
    await run_git("config", "core.hooksPath", "/dev/null", cwd=repo)
    config = (repo / ".git" / "config").read_text()
    if "extraheader" in config.lower() or "authorization" in config.lower():
        raise StepFailed("preparing", "credentials_in_clone", "The clone's git config holds credentials.")


async def _reapply(job: Job, repo: Path, resume: dict[str, Any]) -> None:
    """Changes requested after a push (04 §6): the workspace was deleted, so the new one starts from the pushed
    patch (left uncommitted, so the next patch against base_sha still holds everything) plus the feedback."""
    async with get_sessionmaker()() as db:
        art = await db.get(Artifact, uuid.UUID(resume["patch_artifact_id"]))
        await db.commit()
    path = artifacts.path_of(art) if art is not None else None
    if path is None or not path.is_file():
        raise StepFailed("preparing", "patch_missing", "The pushed changes are no longer kept; start a new job.")
    if path.stat().st_size:
        try:
            await run_git("apply", "--whitespace=nowarn", str(path), cwd=repo)
        except RuntimeError as exc:
            raise StepFailed("preparing", "patch_failed", f"The pushed changes don't apply: {str(exc)[:300]}") from None
    feedback = resume.get("feedback", "")
    # "built": the server wrote feedback.md itself (CI logs, review comments; 08 §F1–F2); "review": the user's words.
    prebuilt = resume.get("kind", "review") != "review"
    (repo / ".mwb" / "feedback.md").write_text(feedback if prebuilt else review_feedback_md(feedback))


async def prepare(ctx: dict[str, Any]) -> None:
    job: Job = ctx["job"]
    if job.status == S.created:
        if await transition(job.id, S.created, S.preparing) is None:
            return
    policy = job.policy
    work = Path(tempfile.mkdtemp(prefix=f"job-{job.id}-", dir=_tmp_root()))
    try:
        await _clone(job, work)
        repo = work / "repo"
        rules = await conventions.from_tree(repo, ctx.get("conventions_md"))  # 08 §F4, at base_sha
        mwb = repo / ".mwb"
        mwb.mkdir(exist_ok=True)
        if rules is not None:
            policy = {**policy, "protected": rules.protected, "conventions_source": rules.source}
            await _set(job.id, policy=policy)
            job.policy = policy
            if rules.source == conventions.SETTINGS_SOURCE:
                (mwb / "conventions.md").write_text(rules.text)
        (mwb / "TASK.md").write_text(
            task_md(ctx["key"], ctx["doc"], rules.source if rules else None, rules.protected if rules else None)
        )
        resume = (job.push or {}).get("resume")
        (mwb / "prompt.md").write_text(
            prompt_md(retry=False, review_feedback=bool(resume), questions_left=questions_left(job))
        )
        if resume:
            await _reapply(job, repo, resume)
        (mwb / "verify.json").write_text(verify_json(ctx["doc"], int(policy["timeout_min"]) * 60))
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        with (info / "exclude").open("a") as f:
            f.write("\n.mwb/\n")  # never part of the patch
        tar = _tar(repo)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        await runner_client.delete_workspace(job.id)  # a re-run prepare starts over
        await runner_client.create_workspace(job.id)
        await runner_client.put_content(job.id, tar)
    except RunnerError as exc:
        raise StepFailed("preparing", f"runner_{exc.code}", exc.message) from None
    extra: dict[str, Any] = {}
    if (job.push or {}).get("resume"):
        extra["push"] = {k: v for k, v in job.push.items() if k != "resume"}
    if await transition(job.id, S.preparing, S.running, attempt=1, current_run=None, **extra) is not None:
        await record_event(job.id, JobEventType.status, {"note": "Workspace ready; starting the agent (attempt 1)."})


def _tmp_root() -> Path:
    root = get_settings().data_dir / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------- run the agent (04 §5.3) ----------


class _Coalescer:
    """At most one event of each type per second; texts are joined, tool uses counted."""

    def __init__(self, job_id: uuid.UUID) -> None:
        self.job_id = job_id
        self.pending: dict[str, dict[str, Any]] = {}
        self.last = 0.0

    def add(self, type_: str, data: dict[str, Any]) -> None:
        cur = self.pending.get(type_)
        if cur is None:
            self.pending[type_] = dict(data)
        elif type_ == "agent_text":
            cur["text"] = (cur["text"] + "\n\n" + data["text"])[-2000:]
        else:
            cur.update(data)
            cur["more"] = cur.get("more", 0) + 1

    async def flush(self, force: bool = False) -> None:
        if not self.pending or (not force and time.monotonic() - self.last < EVENT_INTERVAL_S):
            return
        pending, self.pending = self.pending, {}
        self.last = time.monotonic()
        for type_, data in pending.items():
            await record_event(self.job_id, type_, data)


async def _agent_env(job: Job) -> dict[str, str]:
    key = await keys.get(job.id)
    if not key:
        raise StepFailed("running", "key_missing", "The job's LLM key is gone.")
    return {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_MODEL": "coder", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fast",
            "AGENT_MAX_TURNS": str(job.policy.get("max_turns", 60))}  # fmt: skip


async def _follow(run_id: str, timeout_s: int, on_line) -> dict[str, Any] | None:
    """Follow a run's logs until it ends (or the worker's own deadline passes: then kill it). Returns its status."""
    try:
        async with asyncio.timeout(timeout_s + LOG_GRACE_S):
            async for stream, line in runner_client.logs(run_id, follow=True, timeout=timeout_s + LOG_GRACE_S):
                await on_line(stream, line)
    except TimeoutError:
        await runner_client.kill_run(run_id)
        status = await runner_client.run_status(run_id)
        return {**(status or {}), "state": "killed"}
    for _ in range(30):  # the log stream ends just before Docker reports the exit
        status = await runner_client.run_status(run_id)
        if status is None or status["state"] != "running":
            return status
        await asyncio.sleep(1)
    return await runner_client.run_status(run_id)


QUESTION_CHARS = 2000


def questions_left(job: Job) -> int:
    return max(get_settings().job_max_questions - (job.questions or 0), 0)


async def _asked(job: Job, key: str) -> bool:
    """The agent stopped with `.mwb/question.md` (08 §F3): keep what it did so far and wait for an answer."""
    raw = await runner_client.read_mwb(job.id, "question.md")
    if raw is None:
        return False
    if not questions_left(job):
        await runner_client.delete_mwb(job.id, "question.md")
        await record_event(job.id, JobEventType.error, {"text": "The agent asked past its question limit; ignored."})
        return False
    text = raw.decode(errors="replace").strip()[:QUESTION_CHARS] or "(the question file was empty)"
    patch = await runner_client.patch(job.id, job.base_sha)
    number = (job.questions or 0) + 1
    async with get_sessionmaker()() as db:
        name = f"patch-r{job.review_round}-a{job.attempt}-q{number}.diff"
        art = await artifacts.store(db, job.owner_id, job.id, name, patch, "text/x-diff")
        question = {"text": text, "number": number, "patch_artifact_id": str(art.id),
                    "asked_at": datetime.now(UTC).isoformat()}  # fmt: skip
        moved = await transition_in(db, job.id, S.running, S.needs_input, question=question, questions=number,
                                    current_run=None, detail={"note": "The agent has a question."})  # fmt: skip
        if moved is None:
            await db.rollback()
            return True
        link = f"/work/{job.work_item_id}/job"
        title = f"{key}: the agent has a question"
        add_item(db, job.owner_id, kind="job", severity="warning", title=title,
                 body_md="Answer it on the Job tab; the job waits until you do.", link=link)  # fmt: skip
        user = await db.get(User, job.owner_id)
        await queue_email(db, user, subject=title, body=f"{title}.\n\nAnswer it: {app_url(link)}\n",
                          dedupe_key=f"job-question:{job.id}:{number}")  # fmt: skip
        await db.commit()
    return True


async def run_agent(ctx: dict[str, Any]) -> None:
    job: Job = ctx["job"]
    policy = job.policy
    timeout_s = int(policy["timeout_min"]) * 60
    infra_retry = 0
    while True:
        run_id = job.current_run
        if run_id is None or (await runner_client.run_status(run_id)) is None:
            env = await _agent_env(job)
            try:
                run_id = await runner_client.start_run(
                    job.id, "agent", timeout_s=timeout_s, cpus=policy["cpus"], memory_gb=policy["memory_gb"], env=env,
                    extra_hosts=policy.get("optional_hosts") or [],
                )  # fmt: skip
            except RunnerError as exc:
                raise StepFailed("running", f"runner_{exc.code}", exc.message) from None
            await _set(job.id, current_run=run_id)
            job.current_run = run_id
        outcome = AgentOutcome()
        coalescer = _Coalescer(job.id)
        transcript: list[str] = []

        async def on_line(stream: str, line: str, outcome=outcome, coalescer=coalescer, transcript=transcript) -> None:
            if stream != "stdout":
                return
            transcript.append(line)
            parse_line(line, outcome)
            for type_, data in outcome.events:
                coalescer.add(type_, data)
            outcome.events.clear()
            await coalescer.flush()

        status = await _follow(run_id, timeout_s, on_line)
        await coalescer.flush(force=True)
        await runner_client.kill_run(run_id, remove=True)
        await _set(job.id, current_run=None)
        job.current_run = None
        await _store_text(job, f"agent-transcript-r{job.review_round}-a{job.attempt}.jsonl", "\n".join(transcript),
                          "application/x-ndjson")  # fmt: skip
        await _update_cost(job)
        state, code = (status or {}).get("state"), (status or {}).get("exit_code")
        if outcome.budget_exceeded:
            await _finish(job, S.running, S.budget_exceeded, "running", "budget_exceeded",
                          "The job reached its AI budget.")  # fmt: skip
            return
        if state != "killed" and status is not None and await _asked(job, ctx["key"]):
            return  # 08 §F3: waiting for the reviewer's answer
        if state == "killed" and (status or {}).get("reason") == "disk_limit":
            await _finish(job, S.running, S.failed, "running", "workspace_too_large",
                          "The workspace grew past the sandbox's disk limit.")  # fmt: skip
            return
        if state == "killed" or status is None:
            await _finish(job, S.running, S.timed_out, "running", "timed_out",
                          f"The agent didn't finish within {policy['timeout_min']} minutes.")  # fmt: skip
            return
        if code == 0 and not (outcome.result or {}).get("is_error"):
            await transition(job.id, S.running, S.verifying, current_run=None)
            return
        if outcome.tool_uses == 0 and infra_retry == 0:  # failed before doing anything: likely infrastructure
            infra_retry += 1
            await record_event(job.id, JobEventType.error, {"text": "The agent stopped before starting; trying again."})
            continue
        message = outcome.api_error or f"The agent exited with code {code}."
        await _finish(job, S.running, S.failed, "running", "agent_failed", message)
        return


# ---------- verify (04 §5.4) ----------


async def _wait(run_id: str, timeout_s: int) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s + LOG_GRACE_S
    while time.monotonic() < deadline:
        status = await runner_client.run_status(run_id)
        if status is None or status["state"] != "running":
            return status
        await asyncio.sleep(2)
    await runner_client.kill_run(run_id)
    return {"state": "killed"}


async def verify(ctx: dict[str, Any]) -> None:
    job: Job = ctx["job"]
    policy = job.policy
    timeout_s = int(policy["timeout_min"]) * 60
    run_id = job.current_run
    try:
        if run_id is None or (await runner_client.run_status(run_id)) is None:
            preset = "verify-services" if policy.get("services") else "verify"  # 08 §F10
            run_id = await runner_client.start_run(
                job.id, preset, timeout_s=timeout_s, cpus=policy["cpus"], memory_gb=policy["memory_gb"],
                extra_hosts=policy.get("optional_hosts") or [],
            )  # fmt: skip
            await _set(job.id, current_run=run_id)
        await record_event(job.id, JobEventType.status, {"note": "Running the verification commands."})
        await _wait(run_id, timeout_s)
        await runner_client.kill_run(run_id, remove=True)
        patch = (await runner_client.patch(job.id, job.base_sha)).decode(errors="replace")
        raw = await runner_client.read_mwb(job.id, "verify-result.json")
        results: list[dict[str, Any]] = json.loads(raw) if raw else []
        logs: dict[int, str] = {}
        for n, r in enumerate(results):
            if r.get("log"):
                logs[n] = ((await runner_client.read_mwb(job.id, r["log"].removeprefix(".mwb/"))) or b"").decode(
                    errors="replace"
                )
        notes = ((await runner_client.read_mwb(job.id, "notes.md")) or b"").decode(errors="replace")[:NOTES_CHARS]
    except RunnerError as exc:
        raise StepFailed("verifying", f"runner_{exc.code}", exc.message) from None
    await _set(job.id, current_run=None)
    files = parse_patch(patch)
    checks = await run_checks(patch, files, policy, results)
    for c in checks:
        await record_event(job.id, JobEventType.check, c)
    for r in results:
        await record_event(
            job.id, JobEventType.verify_cmd, {k: r.get(k) for k in ("command", "exit_code", "duration_s", "timed_out")}
        )
    failed = [c for c in checks if c["status"] == "fail"]
    async with get_sessionmaker()() as db:
        patch_row = await artifacts.store(db, job.owner_id, job.id, f"patch-r{job.review_round}-a{job.attempt}.diff",
                                          patch.encode(), "text/x-diff")  # fmt: skip
        verify_rows = []
        for n, r in enumerate(results):
            name = f"verify-r{job.review_round}-a{job.attempt}-{n + 1}.log"
            row = await artifacts.store(db, job.owner_id, job.id, name, logs.get(n, "").encode(), "text/plain")
            verify_rows.append({k: r.get(k) for k in ("command", "exit_code", "duration_s", "timed_out")} | {
                "log_artifact_id": str(row.id)})  # fmt: skip
        await db.commit()
    max_attempts = int(policy.get("max_attempts", 3))
    if failed and job.attempt < max_attempts:
        failing = [(r, logs.get(n, "")) for n, r in enumerate(results) if r.get("exit_code") != 0]
        files = {
            ".mwb/feedback.md": feedback_md(failed, failing),
            ".mwb/prompt.md": prompt_md(retry=True, review_feedback=False, questions_left=questions_left(job)),
        }
        try:
            await runner_client.put_content(job.id, overlay(files))
        except RunnerError as exc:
            raise StepFailed("verifying", f"runner_{exc.code}", exc.message) from None
        note = f"Verification failed; attempt {job.attempt + 1} of {max_attempts}."
        await transition(job.id, S.verifying, S.running, attempt=job.attempt + 1, detail={"note": note})
        return
    cost = await _update_cost(job)
    result = {
        "schema_version": 1, "patch_artifact_id": str(patch_row.id), "patch_sha256": patch_row.sha256,
        "files": [f.public() for f in files], "checks": checks, "verify": verify_rows, "agent_notes": notes,
        "attempts": job.attempt, "cost_usd": float(cost) if cost is not None else float(job.cost_usd or 0),
        "failed_checks": bool(failed),
    }  # fmt: skip
    async with get_sessionmaker()() as db:
        moved = await _rest(db, job, result, ctx)
        if moved:
            await db.commit()


async def _rest(db, job: Job, result: dict[str, Any], ctx: dict[str, Any]) -> bool:
    moved = await transition_in(db, job.id, S.verifying, S.awaiting_review, result=result, current_run=None)
    if moved is None:
        return False
    key = ctx["key"]
    title = f"Job for {key} is ready for review" + (" (checks failed)" if result["failed_checks"] else "")
    link = f"/work/{job.work_item_id}/job"
    body = f"{len(result['files'])} file(s) changed, {result['attempts']} attempt(s)."
    severity = "warning" if result["failed_checks"] else "info"
    add_item(db, job.owner_id, kind="job", severity=severity, title=title, body_md=body, link=link)
    user = await db.get(User, job.owner_id)
    await queue_email(db, user, subject=title, body=f"{title}.\n\nReview it: {app_url(link)}\n",
                      dedupe_key=f"job-review:{job.id}:{job.review_round}")  # fmt: skip
    return True


# ---------- ends and costs ----------


async def _store_text(job: Job, name: str, text: str, mime: str) -> None:
    async with get_sessionmaker()() as db:
        await artifacts.store(db, job.owner_id, job.id, name, text.encode(), mime)
        await db.commit()


async def _update_cost(job: Job) -> Decimal | None:
    try:
        spent = await keys.spend(job.id)
    except Exception:  # noqa: BLE001 - cost is informative; never fail a job over it
        return None
    if spent is not None:
        await _set(job.id, cost_usd=spent)
        job.cost_usd = spent
    return spent


async def cleanup(job_id: uuid.UUID) -> None:
    """When a job leaves the active states (04 §7): its workspace and its LLM key go."""
    for step in (runner_client.delete_workspace, keys.remove):
        try:
            await step(job_id)
        except Exception as exc:  # noqa: BLE001 - cleanup is retried by the runner's sweep and the key's expiry
            log.warning("job.cleanup_failed", job_id=str(job_id), step=step.__name__, error=str(exc)[:200])


async def _finish(job: Job, current: JobStatus, new: JobStatus, stage: str, code: str, message: str) -> None:
    moved = await transition(job.id, current, new, failed_stage=stage, error_code=code, error=message[:2000],
                             current_run=None, detail={"error": message[:500]})  # fmt: skip
    if moved is None:
        return
    async with get_sessionmaker()() as db:
        item = await db.get(WorkItem, job.work_item_id)
        issue = await db.get(JiraIssue, item.jira_issue_id) if item else None
        add_item(db, job.owner_id, kind="job", severity="warning",
                 title=f"Job for {issue.key if issue else 'a ticket'} {new.replace('_', ' ')}", body_md=message[:500],
                 link=f"/work/{job.work_item_id}/job")  # fmt: skip
        await db.commit()
    await cleanup(job.id)


async def advance(job_id: uuid.UUID) -> str | None:
    """The task: do the next steps until the job rests or ends. Returns the final status."""
    for _ in range(50):  # attempts × steps, with room; a loop here is a bug, not a long job
        ctx = await _load(job_id)
        if ctx is None:
            return None
        job: Job = ctx["job"]
        try:
            if job.status in (S.created, S.preparing):
                await prepare(ctx)
            elif job.status == S.running:
                await run_agent(ctx)
            elif job.status == S.verifying:
                await verify(ctx)
            elif job.status in (S.approved_push, S.pushing):
                try:
                    await push_mod.push(job, ctx["key"])
                except push_mod.StepFailed as exc:
                    if exc.code in push_mod.END_CODES:
                        raise StepFailed("pushing", exc.code, exc.message) from None
                    await push_mod.back_to_review(job, ctx["key"], exc.code, exc.message)
                except Exception as exc:  # the approval and patch are intact: let the user retry (08 §F5)
                    log.exception("job.push_crashed", job_id=str(job_id))
                    await push_mod.back_to_review(job, ctx["key"], "internal",
                                                  f"The push stopped unexpectedly ({type(exc).__name__}).")  # fmt: skip
                return (await _load(job_id) or {"job": job})["job"].status
            else:
                return job.status
        except StepFailed as exc:
            log.warning("job.step_failed", job_id=str(job_id), stage=exc.stage, code=exc.code)
            await _finish(job, JobStatus(job.status), S.failed, exc.stage, exc.code, exc.message)
        except Exception as exc:
            log.exception("job.step_crashed", job_id=str(job_id))
            await _finish(job, JobStatus(job.status), S.failed, job.status, "internal",
                          f"The job stopped unexpectedly ({type(exc).__name__}).")  # fmt: skip
    return None
