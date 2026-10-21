"""Upgrade a scratch database one revision at a time with data in it, then check models match the schema.

Design rule R10: every migration must apply on top of real data from the previous revision.
"""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import Connection

from alembic import command
from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.config import get_settings
from app.db import Base

ROOT = Path(__file__).parent.parent
DB_NAME = "mwb_migrations"

# Seed rows written right after the named revision is applied; they must survive every later migration.
SEEDS: dict[str, list[str]] = {
    "0001": [
        "INSERT INTO users (id, username, password_hash, role, is_active) VALUES (1, 'seed', 'x', 'member', true)",
        "INSERT INTO chat_sessions (id, owner_id, title, source, visibility) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 1, 'seed chat', 'chat', 'private')",
        "INSERT INTO messages (session_id, role, content) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'user', 'hi')",
        "INSERT INTO projects (owner_id, visibility, name, repo_url, host, status) "
        "VALUES (1, 'private', 'seed', 'https://git.example/seed.git', 'gitlab', 'ready')",
    ],
    "0002": [
        "INSERT INTO documents (id, owner_id, visibility, source_type, source_ref, title, content_hash, metadata) "
        "VALUES (1, 1, 'private', 'upload', 'upload:seed.md', 'seed.md', 'h', '{}')",
        "INSERT INTO chunks (document_id, owner_id, visibility, ord, content, metadata) "
        "VALUES (1, 1, 'private', 0, 'seed chunk text', '{}')",
    ],
}
SEEDS["0007"] = [
    "INSERT INTO inbox_items (owner_id, kind, severity, title) VALUES (1, 'credential', 'warning', 'seed')",
]
SEEDS["0008"] = [
    "INSERT INTO agents (slug, name, description, model_alias, system_prompt, tools) "
    "VALUES ('seed-agent', 'Seed', 'd', 'fast', 'p', ARRAY['jira_search'])",
]
SEEDS["0009"] = [
    "INSERT INTO schedules (id, owner_id, agent_id, name, cron, timezone) "
    "SELECT '00000000-0000-0000-0000-0000000000aa', 1, id, 'seed', '0 8 * * *', 'UTC' FROM agents LIMIT 1",
    "INSERT INTO runs (schedule_id, owner_id, agent_id, trigger, config, scheduled_for, status) "
    "SELECT '00000000-0000-0000-0000-0000000000aa', 1, id, 'schedule', '{}', now(), 'queued' FROM agents LIMIT 1",
    "INSERT INTO outbound_emails (owner_id, to_addr, subject, body_text, dedupe_key) "
    "VALUES (1, 'a@example.com', 's', 'b', 'seed')",
]
SEEDS["0005"] = []  # 0005 adds a column with a default; the 0001 user must get it
CHECKS = {
    "users": 1,
    "chat_sessions": 1,
    "messages": 1,
    "projects": 1,
    "documents": 1,
    "chunks": 1,
    "inbox_items": 1,
    "agents": 1,
    "schedules": 1,
    "runs": 1,
    "outbound_emails": 1,
}


def _include(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and reflected and compare_to is None:
        return False  # ParadeDB ships extra tables (PostGIS, Procrastinate's own)
    return not (type_ == "index" and name == "chunks_bm25")  # bm25 index options aren't reflectable


@pytest.fixture
def scratch_db() -> Iterator[Connection]:
    base = get_settings().database_url.rsplit("/", 1)[0]
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    url = f"{base}/{DB_NAME}".replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        yield connection
    engine.dispose()
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def _config(connection: Connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def test_upgrade_step_by_step_with_data_and_models_match(scratch_db: Connection) -> None:
    cfg = _config(scratch_db)
    revisions = [s.revision for s in reversed(list(ScriptDirectory.from_config(cfg).walk_revisions()))]
    assert revisions[0] == "0001"
    for rev in revisions:
        command.upgrade(cfg, rev)
        scratch_db.commit()
        for stmt in SEEDS.get(rev, []):
            scratch_db.execute(text(stmt))
        scratch_db.commit()

    for table, expected in CHECKS.items():
        assert scratch_db.execute(text(f"SELECT count(*) FROM {table}")).scalar() == expected, table
    assert scratch_db.execute(text("SELECT timezone_confirmed FROM users WHERE id = 1")).scalar() is False
    # The rebuilt BM25 index (0004) covers rows written before it.
    assert scratch_db.execute(text("SELECT count(*) FROM chunks WHERE content ||| 'seed'")).scalar() == 1

    diff = compare_metadata(MigrationContext.configure(scratch_db, opts={"include_object": _include}), Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_0006_moves_phase_1_tokens_when_urls_are_configured(scratch_db: Connection, monkeypatch) -> None:
    """Design 02 §1: with MWB_GITLAB_URL set, tokens move to user_credentials and projects are linked."""
    cfg = _config(scratch_db)
    command.upgrade(cfg, "0005")
    scratch_db.commit()
    for stmt in [
        "INSERT INTO users (id, username, password_hash, role, is_active) VALUES (1, 'seed', 'x', 'member', true)",
        "INSERT INTO user_secrets (user_id, kind, ciphertext) VALUES (1, 'gitlab_token', 'cipher-g'), "
        "(1, 'jira_token', 'cipher-j'), (1, 'gateway_key', 'cipher-k')",
        "INSERT INTO projects (owner_id, visibility, name, repo_url, host, status) VALUES "
        "(1, 'private', 'on', 'https://gitlab.corp.example/t/a.git', 'gitlab', 'ready'), "
        "(1, 'private', 'off', 'https://elsewhere.example/t/b.git', 'gitlab', 'ready')",
    ]:
        scratch_db.execute(text(stmt))
    scratch_db.commit()
    monkeypatch.setenv("MWB_GITLAB_URL", "https://gitlab.corp.example/")
    monkeypatch.delenv("MWB_JIRA_URL", raising=False)
    monkeypatch.delenv("MWB_GERRIT_URL", raising=False)
    command.upgrade(cfg, "0006")
    scratch_db.commit()
    [(gid, base)] = scratch_db.execute(text("SELECT id, base_url FROM integrations")).all()
    assert base == "https://gitlab.corp.example"
    creds = scratch_db.execute(text("SELECT integration_id, kind, ciphertext, status FROM user_credentials")).all()
    assert creds == [(gid, "token", "cipher-g", "unverified")]  # same keys, so the ciphertext moves as is
    left = sorted(k for (k,) in scratch_db.execute(text("SELECT kind FROM user_secrets")))
    assert left == ["gateway_key", "jira_token"]  # no MWB_JIRA_URL: kept for adoption when Jira is added
    linked = dict(scratch_db.execute(text("SELECT name, integration_id FROM projects")).all())
    assert linked == {"on": gid, "off": None}
