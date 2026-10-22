"""projects.conventions_md and plans.conventions (repository conventions)

Revision ID: 0017
Revises: 0016
Design: docs/design/08-follow-through.md §F4
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("conventions_md", sa.Text()))
    op.create_check_constraint("ck_projects_conventions_md", "projects", "length(conventions_md) <= 20000")
    op.add_column("plans", sa.Column("conventions", JSONB()))
