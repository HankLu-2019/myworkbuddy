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
    change = "change"  # 08 §F7: a merged job's summary


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
    plan = "plan"  # Phase 3: a plan is ready, or drafting failed
    job = "job"  # Phase 4: a job is ready for review, failed, or was pushed


class McpToolStatus(StrEnum):
    """08 §F12: a tool from an admin-registered MCP server."""

    disabled = "disabled"  # listed, not callable (every tool starts here)
    enabled = "enabled"  # an admin enabled it as read, with a reason
    changed = "changed"  # its definition changed on refresh: not callable until an admin looks again
    removed = "removed"  # the server no longer lists it


class ChannelKind(StrEnum):
    """08 §F8: where chat notifications go."""

    slack = "slack"
    teams = "teams"


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


class NotifyMode(StrEnum):
    always = "always"
    on_change = "on_change"
    on_alert = "on_alert"


class RunTrigger(StrEnum):
    schedule = "schedule"
    manual = "manual"  # also test runs of an unsaved schedule (schedule_id null, 02 §6)


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    skipped = "skipped"
    cancelled = "cancelled"


class EmailStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    suppressed = "suppressed"


class ProposalStatus(StrEnum):
    pending = "pending"
    accepted = "accepted"
    dismissed = "dismissed"
    expired = "expired"


class WorkItemState(StrEnum):
    new = "new"
    planning = "planning"
    needs_decision = "needs_decision"
    approved = "approved"
    in_progress = "in_progress"
    in_review = "in_review"
    done = "done"
    dismissed = "dismissed"


class PlanStatus(StrEnum):
    drafting = "drafting"
    needs_decision = "needs_decision"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"
    failed = "failed"


class RevisionAuthor(StrEnum):
    planner = "planner"
    user = "user"


class ApprovalSubject(StrEnum):
    plan_revision = "plan_revision"
    job_push = "job_push"  # Phase 4
    jira_issue = "jira_issue"  # 08 §F6: a Jira issue created from a CI finding


class ApprovalDecision(StrEnum):
    approved = "approved"
    rejected = "rejected"
    changes_requested = "changes_requested"


class JobStatus(StrEnum):
    """Design 04 §3."""

    created = "created"
    preparing = "preparing"
    running = "running"
    verifying = "verifying"
    awaiting_review = "awaiting_review"
    needs_input = "needs_input"  # 08 §F3: the agent asked a question
    approved_push = "approved_push"
    pushing = "pushing"
    pushed = "pushed"
    merged = "merged"
    closed = "closed"
    failed = "failed"
    timed_out = "timed_out"
    budget_exceeded = "budget_exceeded"
    cancelled = "cancelled"
    discarded = "discarded"


class JobEventType(StrEnum):
    status = "status"
    agent_text = "agent_text"
    agent_tool = "agent_tool"
    verify_cmd = "verify_cmd"
    check = "check"
    push = "push"
    error = "error"


def check(column: str, enum: type[StrEnum]) -> str:
    """SQL for a CHECK constraint that keeps `column` within `enum`."""
    values = ", ".join(f"'{e.value}'" for e in enum)
    return f"{column} IN ({values})"
