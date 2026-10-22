"""notify_channels and outbound_messages (Slack and Teams notifications)

Revision ID: 0020
Revises: 0019
Design: docs/design/08-follow-through.md §F8
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op
from app.enums import ChannelKind, EmailStatus, Severity, check

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "notify_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url_enc", sa.Text(), nullable=False),
        sa.Column("url_hint", sa.Text(), nullable=False),
        sa.Column("kinds", ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("min_severity", sa.Text(), server_default="warning", nullable=False),
        sa.Column("include_details", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("disabled_reason", sa.Text()),
        sa.Column("last_item_id", sa.BigInteger(), server_default="0", nullable=False),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_index("ix_notify_channels_owner_id", "notify_channels", ["owner_id"])
    op.create_check_constraint("ck_notify_channels_kind", "notify_channels", check("kind", ChannelKind))
    op.create_check_constraint("ck_notify_channels_min_severity", "notify_channels", check("min_severity", Severity))
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("notify_channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inbox_item_id", sa.BigInteger(), sa.ForeignKey("inbox_items.id", ondelete="SET NULL")),
        sa.Column("dedupe_key", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("link", sa.Text(), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        _now("next_attempt_at"),
        sa.Column("last_error", sa.Text()),
        _now("created_at"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "outbound_messages_due",
        "outbound_messages",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_check_constraint("ck_outbound_messages_status", "outbound_messages", check("status", EmailStatus))
