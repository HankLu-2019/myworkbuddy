"""schedules, runs, outbound_emails; inbox_items.run_id

Revision ID: 0009
Revises: 0008
Design: docs/design/02-schedules-connectors.md §4, §5
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op
from app.enums import EmailStatus, NotifyMode, RunStatus, RunTrigger, check

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _uuid_pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid())


def _ts(name: str, *, now: bool = False) -> sa.Column:
    if now:
        return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    op.create_table(
        "schedules",
        _uuid_pk(),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("inputs", JSONB(), server_default="{}", nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notify_mode", sa.Text(), server_default="on_change", nullable=False),
        sa.Column("notify_email", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("max_runtime_s", sa.Integer(), server_default="300", nullable=False),
        _ts("next_run_at"),
        _ts("last_run_at"),
        sa.Column("last_status", sa.Text()),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paused_reason", sa.Text()),
        sa.Column("created_from_session", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL")),
        _ts("created_at", now=True),
        _ts("updated_at", now=True),
    )
    op.create_index("ix_schedules_owner_id", "schedules", ["owner_id"])
    op.create_index("schedules_due", "schedules", ["next_run_at"], postgresql_where=sa.text("enabled"))
    op.create_check_constraint("ck_schedules_name", "schedules", "length(name) BETWEEN 1 AND 120")
    op.create_check_constraint("ck_schedules_prompt", "schedules", "length(prompt) <= 4000")
    op.create_check_constraint("ck_schedules_notify_mode", "schedules", check("notify_mode", NotifyMode))
    op.create_check_constraint("ck_schedules_max_runtime_s", "schedules", "max_runtime_s BETWEEN 30 AND 1800")

    op.create_table(
        "runs",
        _uuid_pk(),
        sa.Column("schedule_id", UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="SET NULL")),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL")),
        sa.Column("report", JSONB()),
        sa.Column("fingerprints", ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("changed", sa.Boolean()),
        sa.Column("notified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column("error_code", sa.Text()),
        sa.Column("error", sa.Text()),
        _ts("started_at"),
        _ts("finished_at"),
        _ts("created_at", now=True),
        sa.UniqueConstraint("schedule_id", "scheduled_for"),
    )
    op.create_index("runs_owner_created", "runs", ["owner_id", sa.text("created_at DESC")])
    op.create_index("runs_schedule_created", "runs", ["schedule_id", sa.text("created_at DESC")])
    op.create_check_constraint("ck_runs_trigger", "runs", check("trigger", RunTrigger))
    op.create_check_constraint("ck_runs_status", "runs", check("status", RunStatus))

    op.add_column("inbox_items", sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")))

    op.create_table(
        "outbound_emails",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("to_addr", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        _ts("next_attempt_at", now=True),
        sa.Column("last_error", sa.Text()),
        _ts("created_at", now=True),
        _ts("sent_at"),
    )
    op.create_check_constraint("ck_outbound_emails_status", "outbound_emails", check("status", EmailStatus))
    op.create_index(
        "outbound_emails_due", "outbound_emails", ["next_attempt_at"], postgresql_where=sa.text("status = 'pending'")
    )
