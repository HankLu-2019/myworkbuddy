"""Every enum-like column, in one place (rule R9). Migrations add a matching CHECK constraint for each."""

from enum import StrEnum


class Role(StrEnum):
    admin = "admin"
    member = "member"


class Visibility(StrEnum):
    private = "private"
    public = "public"


class ProjectStatus(StrEnum):
    new = "new"
    queued = "queued"
    running = "running"
    ready = "ready"
    failed = "failed"


class GitHost(StrEnum):
    gitlab = "gitlab"
    gerrit = "gerrit"


class SourceType(StrEnum):
    upload = "upload"
    wiki = "wiki"
    episode = "episode"
    code = "code"
    repo_doc = "repo_doc"


class SessionSource(StrEnum):
    chat = "chat"
    claude_code = "claude-code"
    run = "run"
    job = "job"


class ToolCallStatus(StrEnum):
    started = "started"
    done = "done"
    failed = "failed"
    imported = "imported"


class MessageStatus(StrEnum):
    complete = "complete"
    partial = "partial"
    failed = "failed"


class WikiPageStatus(StrEnum):
    ok = "ok"
    missing = "missing"


class LibrarianRunStatus(StrEnum):
    ok = "ok"
    partial = "partial"
    failed = "failed"
    skipped = "skipped"


class IntegrationKind(StrEnum):
    jira = "jira"
    gitlab = "gitlab"
    gerrit = "gerrit"
    jenkins = "jenkins"
    kubernetes = "kubernetes"
    feed = "feed"


class CredentialStatus(StrEnum):
    unverified = "unverified"
    ok = "ok"
    invalid = "invalid"
    unreadable = "unreadable"


class InboxKind(StrEnum):
    run = "run"
    alert = "alert"
    recovered = "recovered"
    system = "system"
    credential = "credential"
    proposal = "proposal"


class Severity(StrEnum):
    info = "info"
    warning = "warning"
    alert = "alert"


class Tier(StrEnum):
    """What a tool may do (design 02 §2). An agent's `max_tier` caps the tools it gets."""

    read = "read"
    sandbox_write = "sandbox_write"  # Phase 4: only inside job sandboxes
    external_write = "external_write"  # Phase 4+: only via approvals (rule R5)


class AgentModel(StrEnum):
    """Model aliases an agent may use (design 02 §3); `coder` is for Phase 4 jobs only."""

    chat = "chat"
    fast = "fast"
    planner = "planner"


def check(column: str, enum: type[StrEnum]) -> str:
    """SQL for a CHECK constraint that keeps `column` within `enum`."""
    values = ", ".join(f"'{e.value}'" for e in enum)
    return f"{column} IN ({values})"
