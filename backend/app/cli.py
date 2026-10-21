import asyncio
import getpass
import os

import typer
from sqlalchemy import select

from app.auth.service import hash_password, normalize_username
from app.config import ensure_runtime_ok, get_settings
from app.db import get_sessionmaker
from app.models import User

# Plain tracebacks: CLI output ends up in container logs and CI annotations, where boxed ones are unreadable.
cli = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)

# Commands that must work before the configuration is complete.
_NO_CONFIG_CHECK = {"generate-key", "check-config", "worker-health"}


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


@cli.command("worker")
def worker_cmd(concurrency: int = typer.Option(4, "--concurrency")) -> None:
    """Run the background worker with its heartbeat (design 01 §H14). SIGTERM finishes running jobs first."""
    from app.ops.health import heartbeat_loop
    from app.tasks.app import app as tasks_app

    async def run() -> None:
        async with tasks_app.open_async():
            beat = asyncio.create_task(heartbeat_loop())
            try:
                await tasks_app.run_worker_async(concurrency=concurrency, install_signal_handlers=True)
            finally:
                beat.cancel()

    asyncio.run(run())


@cli.command("worker-health")
def worker_health() -> None:
    """Exit 1 if the worker's heartbeat is older than 3 minutes (the worker container's healthcheck)."""
    from app.ops.health import HEALTHY_MAX_AGE, HEARTBEAT_KEY, get_states, heartbeat_age

    try:
        state = asyncio.run(get_states(HEARTBEAT_KEY)).get(HEARTBEAT_KEY)
    except Exception as exc:
        typer.echo(f"unhealthy: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    age = heartbeat_age(state)
    if age is None or age > HEALTHY_MAX_AGE:
        typer.echo(
            f"unhealthy: heartbeat {'missing' if age is None else f'{int(age.total_seconds())} s old'}", err=True
        )
        raise typer.Exit(1)
    typer.echo(f"healthy: heartbeat {int(age.total_seconds())} s old")


@cli.command("reingest-all")
def reingest_all() -> None:
    """Queue a re-ingest of every project (after a restore: repo caches aren't backed up)."""
    from app.models import Project
    from app.tasks.app import app as tasks_app
    from app.tasks.app import defer_ingest

    async def run() -> int:
        async with get_sessionmaker()() as db:
            ids = list(await db.scalars(select(Project.id)))
        async with tasks_app.open_async():
            for pid in ids:
                await defer_ingest(pid, force=True)
        return len(ids)

    typer.echo(f"queued {asyncio.run(run())} project(s)")


backup_cli = typer.Typer(help="Backups and restores (design 06 §5, §6)")
cli.add_typer(backup_cli, name="backup")


@backup_cli.command("run")
def backup_run() -> None:
    """Back up now (also: `docker compose run --rm backup now`)."""
    from app.ops.backup import run_backup

    result = asyncio.run(run_backup())
    typer.echo(
        f"snapshot {result.snapshot_id}: {result.sizes}" + (f" (skipped {result.skipped})" if result.skipped else "")
    )


@backup_cli.command("daemon")
def backup_daemon() -> None:
    """Back up every night at MWB_BACKUP_TIME (the backup service's command)."""
    from app.ops.backup import daemon

    asyncio.run(daemon())


@backup_cli.command("list")
def backup_list(snapshot: str = typer.Argument("latest")) -> None:
    """Files in a snapshot."""
    from app.ops.backup import list_snapshot

    for path in asyncio.run(list_snapshot(snapshot)):
        typer.echo(path)


@backup_cli.command("restore")
def backup_restore(
    snapshot: str = typer.Option("latest", "--snapshot"),
    force: bool = typer.Option(False, "--force", help="Overwrite a database that already has users"),
) -> None:
    """Restore databases and /data (deploy/restore.sh runs this, then migrations and the rest)."""
    from app.db_bootstrap import bootstrap
    from app.ops.backup import restore

    for line in bootstrap(get_settings().database_url, os.environ.get("LITELLM_DB_PASSWORD") or None):
        typer.echo(line)
    typer.echo(str(asyncio.run(restore(snapshot, force=force))))


@backup_cli.command("verify-restore")
def backup_verify() -> None:
    """Counts that show a restore worked: users, wiki pages with their files, tokens that still decrypt."""
    import json

    from app.knowledge.wiki import read_page
    from app.models import UserSecret, WikiPage
    from app.secrets.service import readable

    async def run() -> dict:
        async with get_sessionmaker()() as db:
            users = len(list(await db.scalars(select(User.id))))
            pages = list(await db.scalars(select(WikiPage)))
            secrets = list(await db.scalars(select(UserSecret)))
        files_ok = sum(1 for page in pages if read_page(page))
        good = sum(readable(s.ciphertext) for s in secrets)
        return {
            "users": users,
            "wiki_pages": len(pages),
            "wiki_files_ok": files_ok,
            "secrets": len(secrets),
            "secrets_readable": good,
        }

    report = asyncio.run(run())
    typer.echo(json.dumps(report))
    if (
        not report["users"]
        or report["wiki_files_ok"] < report["wiki_pages"]
        or report["secrets_readable"] < report["secrets"]
    ):
        raise typer.Exit(1)


@backup_cli.command("record-drill")
def backup_record_drill(
    ok: bool = typer.Option(..., "--ok/--failed", help="Did the restore drill succeed?"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Record a restore drill for the System page (design 06 §6)."""
    from app.ops.backup import record_drill

    asyncio.run(record_drill(ok, note))
    typer.echo("recorded")


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
