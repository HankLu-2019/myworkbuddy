"""schedule_proposals; chat_sessions.context

Revision ID: 0010
Revises: 0009
Design: docs/design/02-schedules-connectors.md §6
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op
from app.enums import ProposalStatus, check

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("context", JSONB(), server_default="[]", nullable=False))
    op.create_table(
        "schedule_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "session_id", UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("schedule_id", UUID(as_uuid=True), sa.ForeignKey("schedules.id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_schedule_proposals_owner_id", "schedule_proposals", ["owner_id"])
    op.create_check_constraint("ck_schedule_proposals_status", "schedule_proposals", check("status", ProposalStatus))
