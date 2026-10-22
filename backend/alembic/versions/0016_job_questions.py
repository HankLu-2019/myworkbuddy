"""jobs: status needs_input, the agent's open question and how many it asked

Revision ID: 0016
Revises: 0015
Design: docs/design/08-follow-through.md §F3
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.enums import JobStatus, check
from app.models import ACTIVE_JOB_STATUSES

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("question", JSONB()))
    op.add_column("jobs", sa.Column("questions", sa.Integer(), server_default="0", nullable=False))
    # Widening a CHECK is safe for existing rows: every old value is still allowed.
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_status", "jobs", check("status", JobStatus))
    # A job waiting for an answer is still the ticket's one active job.
    op.drop_index("one_active_job_per_item", table_name="jobs")
    active = ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES)
    op.create_index(
        "one_active_job_per_item",
        "jobs",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({active})"),
    )
