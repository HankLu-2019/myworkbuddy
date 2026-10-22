"""jira_issues, work_items; users' Jira intake settings

Revision ID: 0011
Revises: 0010
Design: docs/design/03-plans.md §2, §7
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op
from app.enums import WorkItemState, check

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("jira_intake_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column("users", sa.Column("jira_intake_jql", sa.Text()))
    op.add_column("users", sa.Column("jira_autoplan_label", sa.Text()))
    op.create_table(
        "jira_issues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.Text()),
        sa.Column("priority", sa.Text()),
        sa.Column("updated_remote", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fields", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", "integration_id", "key"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "jira_issue_id", sa.BigInteger(), sa.ForeignKey("jira_issues.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("state", sa.Text(), server_default="new", nullable=False),
        sa.Column("current_plan_id", UUID(as_uuid=True)),
        sa.Column("current_job_id", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_id", "jira_issue_id", "project_id", postgresql_nulls_not_distinct=True),
    )
    op.create_index("work_items_owner_state", "work_items", ["owner_id", "state", sa.text("updated_at DESC")])
    op.create_check_constraint("ck_work_items_state", "work_items", check("state", WorkItemState))
