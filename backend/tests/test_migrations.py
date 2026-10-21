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
CHECKS = {
    "users": 1,
    "chat_sessions": 1,
    "messages": 1,
    "projects": 1,
    "documents": 1,
    "chunks": 1,
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
    # The rebuilt BM25 index (0004) covers rows written before it.
    assert scratch_db.execute(text("SELECT count(*) FROM chunks WHERE content ||| 'seed'")).scalar() == 1

    diff = compare_metadata(MigrationContext.configure(scratch_db, opts={"include_object": _include}), Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"
