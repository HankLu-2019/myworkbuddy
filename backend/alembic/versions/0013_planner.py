"""plans.auto (intake auto-drafts); inbox kind 'plan'

Revision ID: 0013
Revises: 0012
Design: docs/design/03-plans.md §5, §7
"""

import sqlalchemy as sa

from alembic import op
from app.enums import InboxKind, check

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("auto", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    # Widening a CHECK is safe for existing rows: every old value is still allowed.
    op.drop_constraint("ck_inbox_items_kind", "inbox_items", type_="check")
    op.create_check_constraint("ck_inbox_items_kind", "inbox_items", check("kind", InboxKind))
