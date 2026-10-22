"""finding_tickets; approvals may bind a Jira issue

Revision ID: 0018
Revises: 0017
Design: docs/design/08-follow-through.md §F6
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op
from app.enums import ApprovalSubject, check

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finding_tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("jira_key", sa.Text(), nullable=False),
        sa.Column("work_item_id", UUID(as_uuid=True), sa.ForeignKey("work_items.id", ondelete="SET NULL")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", "fingerprint"),
    )
    # Widening a CHECK is safe for existing rows: every old value is still allowed.
    op.drop_constraint("ck_approvals_subject_type", "approvals", type_="check")
    op.create_check_constraint("ck_approvals_subject_type", "approvals", check("subject_type", ApprovalSubject))
