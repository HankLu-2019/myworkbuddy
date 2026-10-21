import asyncio
import getpass
import os

import typer
from sqlalchemy import select

from app.auth.service import hash_password, normalize_username
from app.config import ensure_runtime_ok, get_settings
from app.db import get_sessionmaker
from app.models import User

cli = typer.Typer(no_args_is_help=True)

# Commands that must work before the configuration is complete.
_NO_CONFIG_CHECK = {"generate-key", "check-config"}


@cli.callback()
def _startup(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand not in _NO_CONFIG_CHECK:
        ensure_runtime_ok()


@cli.command("check-config")
def check_config() -> None:
    """Validate configuration; exits non-zero with every problem listed. The worker runs this before starting."""
    ensure_runtime_ok()
    typer.echo("configuration ok")


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


@cli.command("wiki-reconcile")
def wiki_reconcile(dry_run: bool = typer.Option(False, "--dry-run", help="Report only; change nothing")) -> None:
    """Repair drift between the wiki git repos and the database (design 01 §H7)."""
    from app.knowledge.reconcile import reconcile_wiki

    report = asyncio.run(reconcile_wiki(dry_run=dry_run))
    for name, value in vars(report).items():
        typer.echo(f"{name}: {value}")


def main() -> None:
    cli()
