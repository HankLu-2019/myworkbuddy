"""agents: agent definitions (system agents synced from app/agents/seed by `mwb sync-agents`)

Revision ID: 0008
Revises: 0007
Design: docs/design/02-schedules-connectors.md §3
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op
from app.enums import AgentModel, Tier, check

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("model_alias", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tools", ARRAY(sa.Text()), nullable=False),
        sa.Column("max_tier", sa.Text(), server_default="read", nullable=False),
        sa.Column("inputs_schema", JSONB(), server_default="{}", nullable=False),
        sa.Column("report", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint("ck_agents_model_alias", "agents", check("model_alias", AgentModel))
    op.create_check_constraint("ck_agents_max_tier", "agents", check("max_tier", Tier))
    op.create_check_constraint("ck_agents_slug", "agents", "slug ~ '^[a-z][a-z0-9-]{1,47}$'")
