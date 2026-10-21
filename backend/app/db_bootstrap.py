"""Database setup that runs before Alembic, from the one-shot `migrate` service."""

from collections.abc import Iterator

import psycopg
from psycopg import sql

EXTENSIONS = ("vector", "pg_search")
LITELLM_ROLE = "litellm"
LITELLM_DB = "litellm"


def bootstrap(
    database_url: str,
    litellm_password: str | None,
    *,
    role: str = LITELLM_ROLE,
    database: str = LITELLM_DB,
) -> Iterator[str]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        for ext in EXTENSIONS:
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
        yield f"extensions ready: {', '.join(EXTENSIONS)}"

        if not litellm_password:
            yield "LITELLM_DB_PASSWORD not set; skipping the LiteLLM database"
            return
        role_exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if role_exists else "CREATE"
        conn.execute(
            sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.SQL(verb), sql.Identifier(role), sql.Literal(litellm_password)
            )
        )
        yield f"role {role} {'updated' if role_exists else 'created'}"
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            yield f"database {database} exists"
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(role)))
            yield f"database {database} created"
