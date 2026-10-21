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
            user = User(username=name, email=email, role="admin", password_hash=hash_password(password))
            db.add(user)
            await db.commit()
            typer.echo(f"Created admin {name}")
        from app.llm.keys import create_for_new_user

        await create_for_new_user(user.id)  # logged if the gateway isn't up yet; created on first use then

    asyncio.run(run())


@cli.command("reset-password")
def reset_password_cmd(username: str = typer.Option(..., "--username")) -> None:
    """Give a user a new one-time password (printed once) and sign them out everywhere."""
    from app.audit.service import audit
    from app.auth.service import generate_password, revoke_user_sessions

    async def run() -> str:
        async with get_sessionmaker()() as db:
            user = await db.scalar(select(User).where(User.username == normalize_username(username)))
            if user is None:
                typer.echo(f"No user {username}", err=True)
                raise typer.Exit(1)
            password = generate_password()
            user.password_hash = hash_password(password)
            user.must_change_password = True
            user.is_active = True
            await revoke_user_sessions(db, user.id)
            await audit(
                db, "user.reset_password", user_id=None, target_type="user", target_id=user.id, detail={"via": "cli"}
            )
            await db.commit()
            return password

    password = asyncio.run(run())
    typer.echo(f"One-time password for {normalize_username(username)}: {password}")
    typer.echo("They must change it at their next sign-in.")


@cli.command("list-users")
def list_users_cmd() -> None:
    """Username, role, active, must change password, last sign-in."""

    async def run() -> list[User]:
        async with get_sessionmaker()() as db:
            return list(await db.scalars(select(User).order_by(User.username)))

    for u in asyncio.run(run()):
        last = u.last_login_at.isoformat(timespec="minutes") if u.last_login_at else "never"
        flags = ("active" if u.is_active else "disabled") + (", must change password" if u.must_change_password else "")
        typer.echo(f"{u.username}\t{u.role}\t{flags}\tlast sign-in {last}")


@cli.command("rotate-secrets")
def rotate_secrets_cmd() -> None:
    """Re-encrypt stored tokens with the first key in MWB_SECRET_KEYS (06-operations §8)."""
    from app.secrets.service import rotate_secrets

    async def run() -> dict[str, int]:
        async with get_sessionmaker()() as db:
            counts = await rotate_secrets(db)
            await db.commit()
            return counts

    counts = asyncio.run(run())
    typer.echo(f"rotated: {counts['rotated']}, unreadable (users must re-enter): {counts['unreadable']}")


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


@cli.command("reembed")
def reembed_cmd(dry_run: bool = typer.Option(False, "--dry-run", help="Report counts only; change nothing")) -> None:
    """Clear vectors made by another embedding model; the worker re-embeds them (design 01 §H10).

    Run after changing MWB_EMBED_MODEL_ID or the gateway's embedding route. Until refilled, those chunks are
    found by keyword search only.
    """
    from app.knowledge.indexer import reembed

    async def run() -> dict:
        async with get_sessionmaker()() as db:
            return await reembed(db, dry_run=dry_run)

    report = asyncio.run(run())
    typer.echo(f"current model: {report['current_model']}")
    for model, count in sorted(report["other_models"].items()):
        typer.echo(f"other model {model}: {count} chunks")
    typer.echo(f"waiting for a vector: {report['missing']}; failed 5+ times: {report['failed']}")
    typer.echo(f"cleared: {report['cleared']}" + (" (dry run)" if dry_run else ""))


llm_cli = typer.Typer(help="AI gateway: per-user keys (design 01 §H12)")
cli.add_typer(llm_cli, name="llm")


@llm_cli.command("rotate-key")
def llm_rotate_key(user: str = typer.Option(..., "--user", help="username")) -> None:
    """Give a user a new gateway key and delete the old one (06-operations §8)."""
    from app.llm.keys import rotate_key

    async def run() -> None:
        async with get_sessionmaker()() as db:
            row = await db.scalar(select(User).where(User.username == normalize_username(user)))
        if row is None:
            typer.echo(f"No user {user}", err=True)
            raise typer.Exit(1)
        await rotate_key(row.id)

    asyncio.run(run())
    typer.echo(f"Rotated the gateway key for {normalize_username(user)}")


@llm_cli.command("check")
def llm_check() -> None:
    """Create, list and delete a throwaway key on the gateway. Run after install and after upgrading LiteLLM."""
    from app.llm.keys import check_gateway

    problems = asyncio.run(check_gateway())
    for p in problems:
        typer.echo(f"problem: {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("gateway key management ok")


def main() -> None:
    cli()
