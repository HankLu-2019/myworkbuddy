import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from app.config import get_settings
from app.db_bootstrap import bootstrap


@pytest.fixture
def names() -> Iterator[tuple[str, str]]:
    # Throwaway names: the test server may be shared with a dev stack whose real "litellm" role must not change.
    suffix = uuid.uuid4().hex[:8]
    role, database = f"litellm_test_{suffix}", f"litellm_test_{suffix}"
    yield role, database
    admin = get_settings().database_url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_bootstrap_is_idempotent_and_creates_litellm_db(names: tuple[str, str]) -> None:
    role, database = names
    url = get_settings().database_url
    first = list(bootstrap(url, "first-password", role=role, database=database))
    second = list(bootstrap(url, "second-password", role=role, database=database))
    assert first[0].startswith("extensions ready")
    assert f"role {role} created" in first and f"database {database} created" in first
    assert f"role {role} updated" in second and f"database {database} exists" in second
    owner = get_settings().database_url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(owner, autocommit=True) as conn:
        row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (database,)).fetchone()
    assert row == (role,)


def test_bootstrap_without_litellm_password_only_creates_extensions() -> None:
    lines = list(bootstrap(get_settings().database_url, None, role="unused", database="unused"))
    assert lines[-1].startswith("LITELLM_DB_PASSWORD not set")
