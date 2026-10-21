"""inbox: inbox_items (owner, kind, severity, title, body, in-app link)

Revision ID: 0007
Revises: 0006
Design: docs/design/02-schedules-connectors.md §5

Created in S2 so a rejected credential reaches the user (§2 "Error handling"). S4 adds `run_id` when `runs`
exists; S5 adds the API, unread count and email.
"""

import sqlalchemy as sa

from alembic import op
from app.enums import InboxKind, Severity, check

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_md", sa.Text(), server_default="", nullable=False),
        sa.Column("link", sa.Text()),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint("ck_inbox_items_kind", "inbox_items", check("kind", InboxKind))
    op.create_check_constraint("ck_inbox_items_severity", "inbox_items", check("severity", Severity))
    op.create_index(
        "inbox_owner_unread", "inbox_items", ["owner_id", "created_at"], postgresql_where=sa.text("read_at IS NULL")
    )
