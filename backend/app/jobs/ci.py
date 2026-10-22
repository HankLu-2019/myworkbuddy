"""The pipeline of a pushed job, and the feedback that fixes it (design 08 §F1).

`pipeline_state()` asks the git host about the pipeline of the pushed commit: GitLab's merge request pipelines, or
Gerrit's `Verified` vote on the current patch set with the CI message that set it. `failure_feedback()` fetches
the end of the failing jobs' logs (GitLab job traces, or a Jenkins build linked from Gerrit) and renders
`feedback.md` for a new round. Log text is untrusted (rule R6): each log goes in as `untrusted.wrap()` JSON, so
nothing in it can pose as part of the feedback's own text.
"""

import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse

from app.connectors import gitlab, jenkins
from app.connectors.base import Connection, ConnectorError, call_json
from app.jobs.push import _gitlab_project
from app.llm.untrusted import UNTRUSTED_NOTICE, wrap
from app.models import Integration, Job, Project

MAX_FAILED_JOBS = 3
LOG_LINES = 200
LOG_CHARS = 64 * 1024 // MAX_FAILED_JOBS
MESSAGE_CHARS = 4000
JENKINS_BUILD = re.compile(r"https?://[^\s)>\]]+?/job/[^\s)>\]]+?/\d+/?(?=[\s)>\].,]|$)")

GITLAB_STATUS = {"success": "passed", "failed": "failed", "canceled": "canceled", "skipped": "canceled"}


async def pipeline_state(job: Job, conn: Connection, project: Project) -> dict[str, Any] | None:
    """`{sha, status: running|passed|failed|canceled, url, failed: [...]}` for the pushed commit, or None if CI
    hasn't started on it (or can't be read)."""
    push = job.push or {}
    sha = push.get("pushed_sha")
    if not sha:
        return None
    if project.host == "gerrit":
        return await _gerrit_state(job, conn, sha)
    iid = push.get("mr_iid")
    if not iid:
        return None
    pid = _gitlab_project(project, conn.integration)
    pipelines = await call_json(conn, "GET", f"/api/v4/projects/{pid}/merge_requests/{iid}/pipelines") or []
    mine = next((p for p in pipelines if p.get("sha") == sha), None)
    if mine is None:
        return None
    status = GITLAB_STATUS.get(mine.get("status", ""), "running")
    failed: list[dict[str, Any]] = []
    if status == "failed":
        jobs = await call_json(
            conn, "GET", f"/api/v4/projects/{pid}/pipelines/{mine['id']}/jobs", params={"scope[]": "failed"}
        ) or []  # fmt: skip
        failed = [{"name": j.get("name"), "id": j.get("id"), "stage": j.get("stage"), "url": j.get("web_url")}
                  for j in jobs[:10]]  # fmt: skip
    return {"sha": sha, "status": status, "url": mine.get("web_url"), "pipeline_id": mine.get("id"), "failed": failed}


def change_number(push: dict[str, Any]) -> int | None:
    if push.get("change_number"):
        return int(push["change_number"])
    m = re.search(r"/(?:\+|c)/(\d+)/?$", push.get("change_url") or "")
    return int(m.group(1)) if m else None


async def _gerrit_state(job: Job, conn: Connection, sha: str) -> dict[str, Any] | None:
    number = change_number(job.push or {})
    if not number:
        return None
    change = await call_json(
        conn, "GET", f"/a/changes/{quote(str(number))}", params={"o": ["LABELS", "MESSAGES", "CURRENT_REVISION"]}
    ) or {}  # fmt: skip
    if change.get("current_revision") and change["current_revision"] != sha:
        return None  # a newer patch set than ours: not our pipeline
    verified = (change.get("labels") or {}).get("Verified") or {}
    status = "failed" if "rejected" in verified else "passed" if "approved" in verified else None
    if status is None:
        return {"sha": sha, "status": "running", "url": None, "failed": []} if verified else None
    failed: list[dict[str, Any]] = []
    url = None
    if status == "failed":
        current = max((m.get("_revision_number") or 0 for m in change.get("messages") or []), default=0)
        messages = [m for m in change.get("messages") or [] if (m.get("_revision_number") or 0) == current]
        ci = next((m for m in reversed(messages) if "Verified-1" in (m.get("message") or "")), None)
        if ci is None and messages:
            ci = messages[-1]
        text = (ci or {}).get("message") or ""
        build = JENKINS_BUILD.search(text)
        url = build.group(0) if build else None
        failed = [{"name": "Verified -1", "id": None, "stage": None, "url": url, "message": text[:MESSAGE_CHARS]}]
    return {"sha": sha, "status": status, "url": url, "failed": failed}


def _jenkins_job(url: str, integration: Integration) -> tuple[str, int] | None:
    """`https://ci/job/a/job/b/12/` under the integration's base URL → ("a/b", 12)."""
    base = integration.base_url.rstrip("/")
    if not url.startswith(base + "/"):
        return None
    parts = [unquote(p) for p in urlparse(url).path[len(urlparse(base).path) :].strip("/").split("/")]
    if len(parts) < 3 or not parts[-1].isdigit():
        return None
    names = [p for i, p in enumerate(parts[:-1]) if i % 2 == 1 and parts[i - 1] == "job"]
    return ("/".join(names), int(parts[-1])) if names else None


async def failure_logs(
    job: Job, pipeline: dict[str, Any], conn: Connection, project: Project, jenkins_conns: list[Connection]
) -> list[tuple[str, str]]:
    """(name, log tail) for up to three failed jobs. Network only; the caller holds no transaction (R1)."""
    out: list[tuple[str, str]] = []
    for f in (pipeline.get("failed") or [])[:MAX_FAILED_JOBS]:
        name = f.get("name") or "CI"
        try:
            if project.host == "gitlab" and f.get("id"):
                path = unquote(_gitlab_project(project, conn.integration))
                tail = await gitlab.job_log_tail(conn, path, int(f["id"]), LOG_LINES)
                out.append((name, tail["log"]))
                continue
            for jc in jenkins_conns:
                found = _jenkins_job(f.get("url") or "", jc.integration)
                if found:
                    tail = await jenkins.build_log_tail(jc, found[0], found[1], LOG_LINES)
                    out.append((f"{name} ({found[0]} #{found[1]})", tail["log"]))
                    break
            else:
                out.append((name, f.get("message") or "(no log could be read; see the review request)"))
        except ConnectorError as exc:
            out.append((name, f"(the log couldn't be read: {exc.message})"))
    return out


def failure_feedback(pipeline: dict[str, Any], logs: list[tuple[str, str]], note: str | None) -> str:
    lines = [
        "# The merge request's pipeline failed",
        "",
        "Make it pass without weakening, skipping or deleting tests. The failing jobs and the end of their logs",
        "follow. " + UNTRUSTED_NOTICE.replace('Tool results marked "untrusted": true', "The JSON blocks below"),
    ]
    if pipeline.get("url"):
        lines += ["", f"Pipeline: {pipeline['url']}"]
    for name, log in logs:
        block = wrap(f"ci:{name}", log, max_chars=LOG_CHARS)
        lines += ["", f"## {name}", "", "```json", json.dumps(block, indent=1, ensure_ascii=False), "```"]
    if note and note.strip():
        lines += ["", "## From the reviewer", "", note.strip()]
    return "\n".join(lines) + "\n"
