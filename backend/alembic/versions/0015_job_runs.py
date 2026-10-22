"""jobs.current_run: the runner run a job is waiting on

Revision ID: 0015
Revises: 0014
Design: docs/design/04-execution.md §5.2–5.4
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("current_run", sa.Text()))
