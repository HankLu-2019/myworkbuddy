"""jobs, job_secrets, job_events; artifacts.job_id and sha256; inbox kind 'job'

Revision ID: 0014
Revises: 0013
Design: docs/design/04-execution.md §2–3
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op
from app.enums import InboxKind, JobEventType, JobStatus, check
from app.models import ACTIVE_JOB_STATUSES

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "work_item_id", UUID(as_uuid=True), sa.ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("plan_revision_id", UUID(as_uuid=True), sa.ForeignKey("plan_revisions.id"), nullable=False),
        sa.Column("plan_approval_id", UUID(as_uuid=True), sa.ForeignKey("approvals.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("base_branch", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=False),
        sa.Column("branch_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failed_stage", sa.Text()),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("review_round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("policy", JSONB(), nullable=False),
        sa.Column("gateway_key_alias", sa.Text()),
        sa.Column("result", JSONB()),
        sa.Column("push", JSONB()),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("error", sa.Text()),
        _now("created_at"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _now("updated_at"),
    )
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"])
    op.create_check_constraint("ck_jobs_status", "jobs", check("status", JobStatus))
    op.create_check_constraint("ck_jobs_branch_name", "jobs", "length(branch_name) <= 100")
    op.create_check_constraint("ck_jobs_review_round", "jobs", "review_round BETWEEN 0 AND 5")
    active = ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES)
    op.create_index(
        "one_active_job_per_item",
        "jobs",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({active})"),
    )
    op.create_table(
        "job_secrets",
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        _now("created_at"),
    )
    op.create_table(
        "job_events",
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        _now("at"),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
    )
    op.create_check_constraint("ck_job_events_type", "job_events", check("type", JobEventType))
    op.add_column("artifacts", sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE")))
    op.add_column("artifacts", sa.Column("sha256", sa.String(64)))
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])
    # Widening a CHECK is safe for existing rows: every old value is still allowed.
    op.drop_constraint("ck_inbox_items_kind", "inbox_items", type_="check")
    op.create_check_constraint("ck_inbox_items_kind", "inbox_items", check("kind", InboxKind))
