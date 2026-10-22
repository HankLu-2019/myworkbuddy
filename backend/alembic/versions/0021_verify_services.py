"""projects.verify_services (verification with containers, opt-in per repository)

Revision ID: 0021
Revises: 0020
Design: docs/design/08-follow-through.md §F10
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("verify_services", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
