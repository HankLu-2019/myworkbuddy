from sqlalchemy import create_engine, pool

from alembic import context
from app import models  # noqa: F401
from app.config import get_settings
from app.db import Base


def include_object(obj, name, type_, reflected, compare_to):
    # The ParadeDB image ships PostGIS/tiger tables; only manage our own.
    return not (type_ == "table" and reflected and compare_to is None)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass their own connection to migrate a scratch database (tests/test_migrations.py).
    if (connection := context.config.attributes.get("connection")) is not None:
        _run(connection)
        return
    engine = create_engine(get_settings().sync_db_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


run_migrations_online()
