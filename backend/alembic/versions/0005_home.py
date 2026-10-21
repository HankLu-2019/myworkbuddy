"""home: users.timezone_confirmed for the getting-started checklist

Revision ID: 0005
Revises: 0004
Design: docs/design/07-ux.md §5.1

Every user has a default timezone, so "timezone saved" needs its own flag. Additive (rule R10): existing users
start unconfirmed and see the step until they confirm.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone_confirmed", sa.Boolean(), server_default="false", nullable=False))
