import asyncio
import getpass
import os

import typer
from sqlalchemy import select

from app.auth.service import hash_password, normalize_username
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import User

cli = typer.Typer(no_args_is_help=True)


@cli.command("create-admin")
def create_admin(username: str = typer.Option("admin"), email: str | None = typer.Option(None)) -> None:
    """Create an admin user. Reads the password from MWB_ADMIN_PASSWORD or prompts for it."""
    password = os.environ.get("MWB_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 10:
        raise typer.BadParameter("Password must be at least 10 characters")

    async def run() -> None:
        async with get_sessionmaker()() as db:
            name = normalize_username(username)
            if await db.scalar(select(User).where(User.username == name)):
                typer.echo(f"User {name} already exists")
                raise typer.Exit(1)
            db.add(User(username=name, email=email, role="admin", password_hash=hash_password(password)))
            await db.commit()
            typer.echo(f"Created admin {name}")

    asyncio.run(run())


@cli.command("generate-key")
def generate_key() -> None:
    """Print a new value for MWB_SECRET_KEY."""
    from cryptography.fernet import Fernet

    typer.echo(Fernet.generate_key().decode())


@cli.command("db-bootstrap")
def db_bootstrap() -> None:
    """Idempotent database setup that can't live in Alembic (CREATE DATABASE can't run in a transaction).

    Creates the extensions, and the LiteLLM role and database when LITELLM_DB_PASSWORD is set.
    """
    from app.db_bootstrap import bootstrap

    for line in bootstrap(get_settings().database_url, os.environ.get("LITELLM_DB_PASSWORD") or None):
        typer.echo(line)


def main() -> None:
    cli()
