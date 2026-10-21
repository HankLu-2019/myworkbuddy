"""integrations: admin-managed integrations and per-user credentials

Revision ID: 0006
Revises: 0005
Design: docs/design/02-schedules-connectors.md §1

Expand step (rule R10). Creates `integrations` and `user_credentials` and adds `projects.integration_id`.
When MWB_GITLAB_URL / MWB_GERRIT_URL / MWB_JIRA_URL are set, creates those integrations, moves each user's
Phase 1 tokens into `user_credentials` (ciphertext unchanged: same keys) and links projects by URL host.
Tokens that can't be moved stay in `user_secrets`; creating the integration later in Admin adopts them
(app/integrations/service.py). A later contract migration drops what is left.
"""

import os
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.enums import CredentialStatus, IntegrationKind, check

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Phase 1 secret kind -> (integration kind, credential kind). Kept in step with app/integrations/service.py.
LEGACY = {
    "gitlab_token": ("gitlab", "token"),
    "gerrit_username": ("gerrit", "username"),
    "gerrit_password": ("gerrit", "password"),
    "jira_token": ("jira", "token"),
}
ENV = {"gitlab": ("MWB_GITLAB_URL", "GitLab"), "gerrit": ("MWB_GERRIT_URL", "Gerrit"), "jira": ("MWB_JIRA_URL", "Jira")}


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("ca_bundle", sa.Text()),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint("ck_integrations_kind", "integrations", check("kind", IntegrationKind))
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column(
            "integration_id",
            sa.Integer(),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="unverified", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "integration_id", "kind"),
    )
    op.create_check_constraint("ck_user_credentials_status", "user_credentials", check("status", CredentialStatus))
    op.add_column(
        "projects",
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_projects_integration_id", "projects", ["integration_id"])
    _migrate_tokens()


def _migrate_tokens() -> None:
    conn = op.get_bind()
    for kind, (env, name) in ENV.items():
        url = os.environ.get(env, "").strip().rstrip("/")
        if not url:
            continue
        integration_id = conn.execute(
            sa.text("INSERT INTO integrations (kind, name, base_url) VALUES (:k, :n, :u) RETURNING id"),
            {"k": kind, "n": name, "u": url},
        ).scalar_one()
        for legacy, (ikind, ckind) in LEGACY.items():
            if ikind != kind:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO user_credentials (user_id, integration_id, kind, ciphertext) "
                    "SELECT user_id, :i, :c, ciphertext FROM user_secrets WHERE kind = :l"
                ),
                {"i": integration_id, "c": ckind, "l": legacy},
            )
            conn.execute(sa.text("DELETE FROM user_secrets WHERE kind = :l"), {"l": legacy})
        host = urlparse(url).hostname
        if kind in ("gitlab", "gerrit") and host:
            rows = conn.execute(
                sa.text("SELECT id, repo_url FROM projects WHERE host = :k AND integration_id IS NULL"), {"k": kind}
            )
            for pid, repo_url in rows.all():
                if urlparse(repo_url).hostname == host:
                    conn.execute(
                        sa.text("UPDATE projects SET integration_id = :i WHERE id = :p"),
                        {"i": integration_id, "p": pid},
                    )
