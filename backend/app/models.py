import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBED_DIMS = 1024


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()
    timezone: Mapped[str] = mapped_column(String(64), server_default="Europe/Stockholm")
    timezone_confirmed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_notifications: Mapped[bool] = mapped_column(Boolean, server_default="true")
    # Jira intake (design 03 §7)
    jira_intake_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    jira_intake_jql: Mapped[str | None] = mapped_column(Text)
    jira_autoplan_label: Mapped[str | None] = mapped_column(Text)
    llm_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default="false")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = _now()
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), index=True)
    at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class UserSecret(Base):
    __tablename__ = "user_secrets"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _updated()


class Integration(Base):
    """One external system instance, configured by an admin (design 02 §1)."""

    __tablename__ = "integrations"
    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ca_bundle: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated()


class UserCredential(Base):
    """One user's secret for one integration; write-only through the API (design 02 §1)."""

    __tablename__ = "user_credentials"
    __table_args__ = (UniqueConstraint("user_id", "integration_id", "kind"),)
    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="unverified", server_default="unverified")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _updated()


class InboxItem(Base):
    """Something the user should see later (design 02 §5). S2 writes credential notices; S5 adds the API and
    email, S4 adds run links."""

    __tablename__ = "inbox_items"
    __table_args__ = (Index("inbox_owner_unread", "owner_id", "created_at", postgresql_where=text("read_at IS NULL")),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body_md: Mapped[str] = mapped_column(Text, default="", server_default="")
    link: Mapped[str | None] = mapped_column(Text)  # in-app path only, e.g. /settings/integrations?focus=3
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Agent(Base):
    """An agent definition (design 02 §3). Phase 2 has system agents only (`owner_id` null), synced from
    app/agents/seed/*.md by `mwb sync-agents`."""

    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    model_alias: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[str]] = mapped_column(ARRAY(Text))
    max_tier: Mapped[str] = mapped_column(Text, default="read", server_default="read")
    inputs_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    report: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Schedule(Base):
    """A recurring agent run (design 02 §4). `next_run_at` is null while disabled."""

    __tablename__ = "schedules"
    __table_args__ = (Index("schedules_due", "next_run_at", postgresql_where=text("enabled")),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    name: Mapped[str] = mapped_column(Text)  # CHECKs in migration 0009: name 1–120, prompt ≤ 4000, runtime 30–1800
    prompt: Mapped[str] = mapped_column(Text, default="", server_default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    cron: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    notify_mode: Mapped[str] = mapped_column(Text, default="on_change", server_default="on_change")
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    max_runtime_s: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    paused_reason: Mapped[str | None] = mapped_column(Text)  # why the system disabled it (failures, budget)
    created_from_session: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Run(Base):
    """One execution of an agent (design 02 §4). Private to its owner. `config` is the snapshot it runs with."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for"),
        Index("runs_owner_created", "owner_id", text("created_at DESC")),
        Index("runs_schedule_created", "schedule_id", text("created_at DESC")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL")
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    trigger: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    fingerprints: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    changed: Mapped[bool | None] = mapped_column(Boolean)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class ScheduleProposal(Base):
    """A schedule the chat model suggested (design 02 §6). Only the user's click turns it into a schedule."""

    __tablename__ = "schedule_proposals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE")
    )
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class JiraIssue(Base):
    """A per-user cache of Jira issues (design 03 §2), refreshed by intake and when a work item is opened."""

    __tablename__ = "jira_issues"
    __table_args__ = (UniqueConstraint("owner_id", "integration_id", "key"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    issue_type: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(Text)
    updated_remote: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB)  # description, labels, components, links, assignee
    fetched_at: Mapped[datetime] = _now()


class WorkItem(Base):
    """One Jira issue + one repository for one user (design 03 §1). Private to its owner."""

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("owner_id", "jira_issue_id", "project_id", postgresql_nulls_not_distinct=True),
        Index("work_items_owner_state", "owner_id", "state", text("updated_at DESC")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    jira_issue_id: Mapped[int] = mapped_column(ForeignKey("jira_issues.id", ondelete="CASCADE"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(Text, default="new", server_default="new")
    current_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="SET NULL", use_alter=True, name="fk_work_items_current_plan"),
    )
    current_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # Phase 4
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Plan(Base):
    """The planning thread for a work item (design 03 §1). Revisions are immutable."""

    __tablename__ = "plans"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    base_sha: Mapped[str | None] = mapped_column(Text)
    base_branch: Mapped[str | None] = mapped_column(Text)
    auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # started by intake
    # 08 §F4: the repository conventions read at the base commit ({source, protected, test}), for validation.
    conventions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class PlanRevision(Base):
    """One complete PlanDoc (design 03 §2). Never edited: any change is a new revision."""

    __tablename__ = "plan_revisions"
    __table_args__ = (UniqueConstraint("plan_id", "revision"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(Text)
    change_note: Mapped[str | None] = mapped_column(Text)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = _now()


ACTIVE_JOB_STATUSES = (
    "created", "preparing", "running", "verifying", "awaiting_review", "needs_input", "approved_push", "pushing",
)  # fmt: skip


class Job(Base):
    """A sandboxed coding job for an approved plan revision (design 04 §2)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "one_active_job_per_item",
            "work_item_id",
            unique=True,
            postgresql_where=text("status IN (" + ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES) + ")"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    work_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("work_items.id", ondelete="CASCADE"))
    plan_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("plan_revisions.id"))
    plan_approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("approvals.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    base_branch: Mapped[str] = mapped_column(Text)
    base_sha: Mapped[str] = mapped_column(Text)
    branch_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    failed_stage: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    review_round: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB)
    gateway_key_alias: Mapped[str | None] = mapped_column(Text)
    current_run: Mapped[str | None] = mapped_column(Text)  # the runner run being waited on (agent or verify)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    push: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 08 §F3: the agent's open question ({text, number, patch_artifact_id, asked_at}) and how many it has asked.
    question: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    questions: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()


class JobSecret(Base):
    """The job's LLM key, encrypted; deleted when the job leaves an active state (04 §2)."""

    __tablename__ = "job_secrets"
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class JobEvent(Base):
    __tablename__ = "job_events"
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = _now()
    type: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class FindingTicket(Base):
    """The Jira issue made from a CI finding (08 §F6), so the same finding opens it instead of making another."""

    __tablename__ = "finding_tickets"
    __table_args__ = (UniqueConstraint("owner_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(Text)
    jira_key: Mapped[str] = mapped_column(Text)
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()


class Approval(Base):
    """A decision bound to (subject, payload_hash) (design 03 §2, rule R5). Append-only: a trigger refuses UPDATE,
    and DELETE except through the cascade from the owning user."""

    __tablename__ = "approvals"
    __table_args__ = (Index("approvals_subject", "subject_type", "subject_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload_hash: Mapped[str] = mapped_column(String(64))
    acknowledged: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    decision: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = _now()


class NotifyChannel(Base):
    """A user's Slack or Teams incoming webhook (08 §F8). The URL is a secret: stored encrypted, shown as a hint."""

    __tablename__ = "notify_channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    url_enc: Mapped[str] = mapped_column(Text)
    url_hint: Mapped[str] = mapped_column(Text)
    kinds: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")  # empty: every kind
    min_severity: Mapped[str] = mapped_column(Text, default="warning", server_default="warning")
    include_details: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    last_item_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")  # inbox items seen
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class OutboundMessage(Base):
    """The chat outbox (08 §F8): one row per inbox item and channel, sent by `send_messages`."""

    __tablename__ = "outbound_messages"
    __table_args__ = (Index("outbound_messages_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("notify_channels.id", ondelete="CASCADE"))
    inbox_item_id: Mapped[int | None] = mapped_column(ForeignKey("inbox_items.id", ondelete="SET NULL"))
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = _now()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundEmail(Base):
    """The email outbox (design 02 §5). S4 queues rows; the sender (`send_emails`) arrives with S5."""

    __tablename__ = "outbound_emails"
    __table_args__ = (Index("outbound_emails_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    to_addr: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = _now()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "repo_url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visibility: Mapped[str] = mapped_column(String(8), default="private")
    name: Mapped[str] = mapped_column(String(128))
    repo_url: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(String(16))
    branch: Mapped[str | None] = mapped_column(String(128))
    last_ingested_sha: Mapped[str | None] = mapped_column(String(64))
    card_page_id: Mapped[int | None] = mapped_column(Integer)
    # The integration whose credentials clone it; null only for projects the 0006 migration couldn't match.
    integration_id: Mapped[int | None] = mapped_column(ForeignKey("integrations.id", ondelete="SET NULL"), index=True)
    conventions_md: Mapped[str | None] = mapped_column(Text)  # 08 §F4, for repositories without a rules file
    status: Mapped[str] = mapped_column(String(16), default="new")
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated()


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "source", "external_ref"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="New chat")
    source: Mapped[str] = mapped_column(String(16), default="chat")
    external_ref: Mapped[str | None] = mapped_column(String(128))
    visibility: Mapped[str] = mapped_column(String(8), default="private")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Things the user attached for the model to see, e.g. [{"type": "run", "id": "<uuid>"}] (design 02 §6).
    context: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_id_id", "session_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[list[Any] | None] = mapped_column(JSONB)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    model: Mapped[str | None] = mapped_column(String(128))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String(16), default="complete", server_default="complete")
    created_at: Mapped[datetime] = _now()


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="started")
    created_at: Mapped[datetime] = _now()


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_sessions.id", ondelete="SET NULL"))
    # Phase 4 (04 §2): transcripts, patches, verify logs and check reports of a job.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    preview: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("owner_id", "source_type", "source_ref"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visibility: Mapped[str] = mapped_column(String(8), default="private")
    source_type: Mapped[str] = mapped_column(String(16))
    source_ref: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    # SET NULL: removing a project deletes its code index explicitly, but other users' session summaries that
    # mention it must survive.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    published_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Defined in migration 0004 (ICU + Jieba tokenizers); expression columns aren't expressible here.
        Index("chunks_bm25", "id", "content", "owner_id", "visibility", postgresql_using="bm25"),
        Index("chunks_missing_embedding", "id", postgresql_where=text("embedding IS NULL AND embed_attempts < 5")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    visibility: Mapped[str] = mapped_column(String(8))
    ord: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIMS))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    embed_model: Mapped[str | None] = mapped_column(String(128))
    embed_attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    embed_error: Mapped[str | None] = mapped_column(Text)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        Index("wiki_private_path", "owner_id", "path", unique=True, postgresql_where=text("visibility = 'private'")),
        Index("wiki_public_path", "path", unique=True, postgresql_where=text("visibility = 'public'")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visibility: Mapped[str] = mapped_column(String(8), default="private")
    path: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="ok", server_default="ok")
    updated_at: Mapped[datetime] = _updated()


class Episode(Base):
    __tablename__ = "episodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), unique=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    visibility: Mapped[str] = mapped_column(String(8), default="private")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated()


class LibrarianState(Base):
    __tablename__ = "librarian_state"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    episodes_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LibrarianRun(Base):
    __tablename__ = "librarian_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    episodes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pages_updated: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    pages_rejected: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)


class SystemState(Base):
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = _updated()
