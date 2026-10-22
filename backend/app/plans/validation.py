"""Deterministic plan validation (design 03 §4). Runs after every revision, the planner's or the user's, and
never calls an LLM. Approve stays disabled unless `ok` and every `needs_ack` item is acknowledged.

`validate_plan` is pure: the caller supplies what it needs from the outside world (the tracked paths at the
plan's base commit, the knowledge ids the owner can see, the admin's optional sandbox hosts), so the same
document always gives the same result.
"""

import posixpath
import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.acl import visible_filter
from app.models import Document, User
from app.plans.schema import PlanDoc

MAX_PATH = 300
MAX_COMMAND = 500
LARGE_CHANGE_FILES = 30
CI_PATTERNS = (
    re.compile(r"(^|/)\.gitlab-ci\.yml$"),
    re.compile(r"(^|/)Jenkinsfile[^/]*$"),
    re.compile(r"^\.github/"),
    re.compile(r"^(deploy|helm|k8s)/"),
    re.compile(r"\.tf$"),
)
NETWORK_TOOLS = re.compile(r"(^|[\s;&|(`])(curl|wget|ssh|scp|nc)(\s|$)")


class Issue(BaseModel):
    level: Literal["error", "warning"]
    field: str
    message: str


class ValidationResult(BaseModel):
    ok: bool
    issues: list[Issue]
    needs_ack: list[str]  # stable keys, e.g. "ci_config:.gitlab-ci.yml", "optional_host:github.com", "large_change"


class _Collector:
    def __init__(self) -> None:
        self.issues: list[Issue] = []
        self.acks: list[str] = []

    def error(self, field: str, message: str) -> None:
        self.issues.append(Issue(level="error", field=field, message=message))

    def warn(self, field: str, message: str, ack: str | None = None) -> None:
        self.issues.append(Issue(level="warning", field=field, message=message))
        if ack and ack not in self.acks:
            self.acks.append(ack)


def normalise_path(path: str) -> tuple[str, str | None]:
    """(normalised path, problem or None)."""
    raw = path.strip()
    if not raw:
        return raw, "is empty"
    if "\\" in raw or "\0" in raw:
        return raw, "must use forward slashes"
    if raw.startswith("/"):
        return raw, "must be relative to the repository root"
    if ".." in raw.split("/"):
        return raw, "must not contain .."
    norm = posixpath.normpath(raw)
    if norm in (".", "") or norm.startswith("../"):
        return raw, "is not a file in the repository"
    if norm == ".git" or norm.startswith(".git/"):
        return norm, "must not be under .git/"
    if len(norm) > MAX_PATH:
        return norm, f"is longer than {MAX_PATH} characters"
    return norm, None


def validate_plan(
    doc: PlanDoc,
    *,
    jira_key: str,
    existing_paths: set[str] | None,
    visible_refs: Iterable[int],
    optional_hosts: Iterable[str],
) -> tuple[PlanDoc, ValidationResult]:
    """The document with paths normalised and invisible knowledge refs dropped, and what's wrong with it.

    `existing_paths` is None when the repository couldn't be read; then existence isn't checked (a warning says so).
    """
    out = _Collector()
    clean = doc.model_copy(deep=True)

    seen: dict[str, int] = {}
    for i, change in enumerate(clean.changes):
        where = f"changes[{i}].path"
        norm, problem = normalise_path(change.path)
        change.path = norm
        if problem:
            out.error(where, f"{change.path!r} {problem}.")
            continue
        if norm in seen:
            out.error(where, f"{norm} is listed twice (also changes[{seen[norm]}]).")
        seen.setdefault(norm, i)
        if existing_paths is not None:
            exists = norm in existing_paths
            if change.action in ("modify", "delete") and not exists:
                out.error(where, f"{norm} doesn't exist at the plan's base commit, so it can't be {change.action}d.")
            if change.action == "create" and exists:
                out.error(where, f"{norm} already exists at the plan's base commit; use modify.")
        if any(p.search(norm) for p in CI_PATTERNS):
            out.warn(where, f"{norm} is CI/CD or deployment configuration.", ack=f"ci_config:{norm}")
    if existing_paths is None:
        out.warn("changes", "The repository couldn't be read, so file paths weren't checked against it.")

    first_line = clean.commit_message.split("\n", 1)[0]
    if len(first_line) > 72:
        out.error("commit_message", f"The first line is {len(first_line)} characters; keep it to 72.")
    if not first_line.startswith(jira_key):
        out.error("commit_message", f"The first line must start with the Jira key {jira_key}.")

    allowed = {h.lower() for h in optional_hosts}
    for i, host in enumerate(clean.sandbox.optional_hosts):
        where = f"sandbox.optional_hosts[{i}]"
        if host.strip().lower() not in allowed:
            out.error(where, f"{host} isn't one of the hosts an admin allows for sandboxes.")
        else:
            out.warn(where, f"The sandbox will be able to reach {host}.", ack=f"optional_host:{host.strip().lower()}")

    for i, command in enumerate(clean.verification):
        where = f"verification[{i}]"
        if len(command) > MAX_COMMAND:
            out.error(where, f"Command is longer than {MAX_COMMAND} characters.")
        if NETWORK_TOOLS.search(command):
            out.warn(where, "Command uses a network tool (curl, wget, ssh, scp or nc).")

    visible = set(visible_refs)
    dropped = [r for r in clean.knowledge_refs if r not in visible]
    if dropped:
        clean.knowledge_refs = [r for r in clean.knowledge_refs if r in visible]
        out.warn("knowledge_refs", f"Dropped references you can't see: {', '.join(map(str, dropped))}.")

    if len(clean.changes) > LARGE_CHANGE_FILES or clean.estimate == "XL":
        out.warn("changes", "Large change; consider splitting it into smaller tickets.", ack="large_change")

    ok = not any(i.level == "error" for i in out.issues)
    return clean, ValidationResult(ok=ok, issues=out.issues, needs_ack=out.acks)


async def visible_document_ids(db: AsyncSession, user: User, ids: Iterable[int]) -> set[int]:
    """Which of these knowledge documents the user can see (rule R2: through app.knowledge.acl)."""
    wanted = list(set(ids))
    if not wanted:
        return set()
    return set(await db.scalars(select(Document.id).where(Document.id.in_(wanted), visible_filter(Document, user))))
