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


def check(column: str, enum: type[StrEnum]) -> str:
    """SQL for a CHECK constraint that keeps `column` within `enum`."""
    values = ", ".join(f"'{e.value}'" for e in enum)
    return f"{column} IN ({values})"
