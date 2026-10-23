"""mcp_servers and mcp_tools (admin-registered MCP servers, read tier)

Revision ID: 0023
Revises: 0022
Design: docs/design/08-follow-through.md §F12
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op
from app.enums import McpToolStatus, check

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("auth_header", sa.Text()),
        sa.Column("auth_value_enc", sa.Text()),
        sa.Column("roles", ARRAY(sa.Text()), server_default="{admin,member}", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_check_constraint("ck_mcp_servers_name", "mcp_servers", "name ~ '^[a-z0-9][a-z0-9-]{1,31}$'")
    op.create_check_constraint("ck_mcp_servers_roles", "mcp_servers", "roles <@ ARRAY['admin', 'member']::text[]")
    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("input_schema", JSONB(), server_default="{}", nullable=False),
        sa.Column("annotations", JSONB(), server_default="{}", nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("write_hint", sa.Text()),
        sa.Column("status", sa.Text(), server_default="disabled", nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("enabled_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
        _now("updated_at"),
        sa.UniqueConstraint("server_id", "name"),
    )
    op.create_check_constraint("ck_mcp_tools_status", "mcp_tools", check("status", McpToolStatus))
    # A write-hinted tool is never enabled (08 §F12: writes need per-call approvals, Phase 6 at the earliest).
    op.create_check_constraint("ck_mcp_tools_read_only", "mcp_tools", "status <> 'enabled' OR write_hint IS NULL")
