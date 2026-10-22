"""plans, plan_revisions, approvals (append-only); work_items.current_plan_id → plans

Revision ID: 0012
Revises: 0011
Design: docs/design/03-plans.md §2
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op
from app.enums import ApprovalDecision, ApprovalSubject, PlanStatus, RevisionAuthor, check

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Rows change only by insert. A delete is allowed only from the cascade of a user being deleted, which runs
# inside the foreign key's own trigger (depth 2); a direct DELETE runs this at depth 1.
IMMUTABLE = """
CREATE FUNCTION approvals_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'approvals are append-only' USING ERRCODE = 'restrict_violation';
  END IF;
  IF TG_OP = 'DELETE' AND pg_trigger_depth() < 2 THEN
    RAISE EXCEPTION 'approvals are removed only with their owner' USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER approvals_append_only BEFORE UPDATE OR DELETE ON approvals
  FOR EACH ROW EXECUTE FUNCTION approvals_append_only();
"""


def _uuid_pk() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid())


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "plans",
        _uuid_pk(),
        sa.Column(
            "work_item_id", UUID(as_uuid=True), sa.ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("base_sha", sa.Text()),
        sa.Column("base_branch", sa.Text()),
        sa.Column("error", sa.Text()),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_index("ix_plans_work_item_id", "plans", ["work_item_id"])
    op.create_check_constraint("ck_plans_status", "plans", check("status", PlanStatus))
    op.create_table(
        "plan_revisions",
        _uuid_pk(),
        sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text()),
        sa.Column("validation", JSONB(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL")),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        _now("created_at"),
        sa.UniqueConstraint("plan_id", "revision"),
    )
    op.create_check_constraint("ck_plan_revisions_author", "plan_revisions", check("author", RevisionAuthor))
    op.create_check_constraint("ck_plan_revisions_hash", "plan_revisions", "content_hash ~ '^[0-9a-f]{64}$'")
    op.create_table(
        "approvals",
        _uuid_pk(),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", UUID(as_uuid=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("acknowledged", ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("ip", sa.Text()),
        _now("decided_at"),
    )
    op.create_index("approvals_subject", "approvals", ["subject_type", "subject_id"])
    op.create_check_constraint("ck_approvals_subject_type", "approvals", check("subject_type", ApprovalSubject))
    op.create_check_constraint("ck_approvals_decision", "approvals", check("decision", ApprovalDecision))
    op.execute(IMMUTABLE)
    op.create_foreign_key(
        "fk_work_items_current_plan", "work_items", "plans", ["current_plan_id"], ["id"], ondelete="SET NULL"
    )
