"""hardening: CHECK constraints, user/session/chunk/message columns, librarian_runs, system_state

Revision ID: 0003
Revises: 0002
Design: docs/design/01-hardening.md §H5
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.enums import (
    GitHost,
    MessageStatus,
    ProjectStatus,
    Role,
    SessionSource,
    SourceType,
    ToolCallStatus,
    Visibility,
    WikiPageStatus,
    check,
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# (table, constraint name, column, enum)
CHECKS = [
    ("users", "ck_users_role", "role", Role),
    ("projects", "ck_projects_visibility", "visibility", Visibility),
    ("projects", "ck_projects_status", "status", ProjectStatus),
    ("projects", "ck_projects_host", "host", GitHost),
    ("chat_sessions", "ck_chat_sessions_visibility", "visibility", Visibility),
    ("chat_sessions", "ck_chat_sessions_source", "source", SessionSource),
    ("tool_calls", "ck_tool_calls_status", "status", ToolCallStatus),
    ("documents", "ck_documents_visibility", "visibility", Visibility),
    ("documents", "ck_documents_source_type", "source_type", SourceType),
    ("chunks", "ck_chunks_visibility", "visibility", Visibility),
    ("wiki_pages", "ck_wiki_pages_visibility", "visibility", Visibility),
    ("episodes", "ck_episodes_visibility", "visibility", Visibility),
]


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Stockholm"))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("email_notifications", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("users", sa.Column("llm_budget_usd", sa.Numeric(10, 2), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default="false"))

    op.add_column(
        "auth_sessions",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("auth_sessions", sa.Column("ip", sa.String(64), nullable=True))
    op.add_column("auth_sessions", sa.Column("user_agent", sa.String(255), nullable=True))

    op.add_column("projects", sa.Column("progress", sa.Text(), nullable=True))

    op.add_column("messages", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column("messages", sa.Column("status", sa.String(16), nullable=False, server_default="complete"))
    op.create_check_constraint("ck_messages_status", "messages", check("status", MessageStatus))
    op.create_index("ix_messages_session_id_id", "messages", ["session_id", "id"])
    op.create_index(op.f("ix_tool_calls_message_id"), "tool_calls", ["message_id"])
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"])

    op.add_column("chunks", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column("chunks", sa.Column("embed_model", sa.String(128), nullable=True))
    op.add_column("chunks", sa.Column("embed_attempts", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("chunks", sa.Column("embed_error", sa.Text(), nullable=True))
    op.create_index("chunks_missing_embedding", "chunks", ["id"], postgresql_where=sa.text("embedding IS NULL"))

    op.add_column("wiki_pages", sa.Column("status", sa.String(16), nullable=False, server_default="ok"))
    op.create_check_constraint("ck_wiki_pages_status", "wiki_pages", check("status", WikiPageStatus))

    for table, name, column, enum in CHECKS:
        op.create_check_constraint(name, table, check(column, enum))

    op.create_table(
        "librarian_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("episodes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_updated", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("pages_rejected", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('ok', 'partial', 'failed', 'skipped')", name="ck_librarian_runs_status"),
    )
    op.create_index(op.f("ix_librarian_runs_user_id"), "librarian_runs", ["user_id"])

    op.create_table(
        "system_state",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # Safety net for rule R1: a session left idle inside a transaction is ended after a minute.
    op.execute("ALTER ROLE CURRENT_USER SET idle_in_transaction_session_timeout = '60s'")


def downgrade() -> None:
    op.execute("ALTER ROLE CURRENT_USER RESET idle_in_transaction_session_timeout")
    op.drop_table("system_state")
    op.drop_index(op.f("ix_librarian_runs_user_id"), table_name="librarian_runs")
    op.drop_table("librarian_runs")
    for table, name, _, _ in reversed(CHECKS):
        op.drop_constraint(name, table, type_="check")
    op.drop_constraint("ck_wiki_pages_status", "wiki_pages", type_="check")
    op.drop_column("wiki_pages", "status")
    op.drop_index("chunks_missing_embedding", table_name="chunks")
    for col in ("embed_error", "embed_attempts", "embed_model", "content_hash"):
        op.drop_column("chunks", col)
    op.drop_index(op.f("ix_audit_log_at"), table_name="audit_log")
    op.drop_index(op.f("ix_tool_calls_message_id"), table_name="tool_calls")
    op.drop_index("ix_messages_session_id_id", table_name="messages")
    op.drop_constraint("ck_messages_status", "messages", type_="check")
    op.drop_column("messages", "status")
    op.drop_column("messages", "cost_usd")
    op.drop_column("projects", "progress")
    for col in ("user_agent", "ip", "last_seen_at"):
        op.drop_column("auth_sessions", col)
    for col in ("must_change_password", "llm_budget_usd", "email_notifications", "last_login_at", "timezone"):
        op.drop_column("users", col)
