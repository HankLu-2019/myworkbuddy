"""users.oidc_sub (single sign-on)

Revision ID: 0022
Revises: 0021
Design: docs/design/08-follow-through.md §F11
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oidc_sub", sa.Text()))
    op.create_unique_constraint("users_oidc_sub_key", "users", ["oidc_sub"])
