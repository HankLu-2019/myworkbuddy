"""Integrations and per-user credentials (design 02 §1). Credentials are write-only: nothing here returns one
to the API; connectors and git read them through get_credentials()."""

from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.integrations.kinds import CREDENTIALS
from app.integrations.kubeconfig import validate_kubeconfig
from app.logs import log
from app.models import Integration, UserCredential, UserSecret
from app.secrets.service import decrypt, encrypt

# Phase 1 secret kind -> (integration kind, credential kind); the same table as migration 0006.
LEGACY = {
    "gitlab_token": ("gitlab", "token"),
    "gerrit_username": ("gerrit", "username"),
    "gerrit_password": ("gerrit", "password"),
    "jira_token": ("jira", "token"),
}


def credential_label(integration: Integration, kind: str) -> str:
    return f"{integration.name} {CREDENTIALS.get(integration.kind, {}).get(kind, kind).lower()}"


async def set_credential(db: AsyncSession, user_id: int, integration: Integration, kind: str, value: str) -> None:
    """Store (or replace) a credential as unverified. The caller audits and commits."""
    if kind not in CREDENTIALS.get(integration.kind, {}):
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{integration.name} has no credential {kind!r}")
    value = value.strip() if kind != "kubeconfig" else value
    if kind == "kubeconfig":
        value = validate_kubeconfig(value, integration.base_url)
    row = await db.scalar(
        select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.integration_id == integration.id,
            UserCredential.kind == kind,
        )
    )
    if row is None:
        row = UserCredential(user_id=user_id, integration_id=integration.id, kind=kind, ciphertext="")
        db.add(row)
    row.ciphertext = encrypt(value)
    row.status, row.verified_at, row.last_error = "unverified", None, None
    # A changed part of a multi-part credential (Gerrit username + password) makes the others unverified too.
    for other in await db.scalars(
        select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.integration_id == integration.id,
            UserCredential.kind != kind,
        )
    ):
        if other.status == "ok":
            other.status = "unverified"


async def credential_rows(db: AsyncSession, user_id: int, integration_id: int) -> dict[str, UserCredential]:
    rows = await db.scalars(
        select(UserCredential).where(UserCredential.user_id == user_id, UserCredential.integration_id == integration_id)
    )
    return {r.kind: r for r in rows}


async def get_credentials(db: AsyncSession, user_id: int, integration: Integration) -> dict[str, str]:
    """Decrypted credentials for one integration. Unreadable ones raise 409 credential_invalid."""
    out: dict[str, str] = {}
    for kind, row in (await credential_rows(db, user_id, integration.id)).items():
        value = decrypt(row.ciphertext)
        if value is None:
            raise AppError(
                status.HTTP_409_CONFLICT,
                "credential_invalid",
                f"Your {credential_label(integration, kind)} can't be read. Enter it again in Settings → Integrations.",
            )
        out[kind] = value
    return out


def state_of(row: UserCredential | None) -> str:
    if row is None:
        return "not_set"
    return "unreadable" if decrypt(row.ciphertext) is None else row.status


async def record_verification(
    db: AsyncSession, user_id: int, integration_id: int, *, ok: bool, auth_failed: bool, message: str
) -> str:
    """Update every credential part of the integration after a test; returns the new status. Caller commits."""
    now = datetime.now(UTC)
    new = "ok" if ok else ("invalid" if auth_failed else None)  # an outage says nothing about the credential
    rows = list((await credential_rows(db, user_id, integration_id)).values())
    for row in rows:
        if new:
            row.status = new
        row.last_error = None if ok else message[:500]
        if ok:
            row.verified_at = now
    return new or (rows[0].status if rows else "unverified")


def _host(url: str) -> str | None:
    return (urlparse(url).hostname or "").lower() or None


async def integration_for_repo(db: AsyncSession, kind: str, repo_url: str) -> Integration | None:
    """The enabled integration of `kind` serving repo_url's host; the longest matching base_url wins."""
    host = _host(repo_url)
    candidates = [
        i
        for i in await db.scalars(select(Integration).where(Integration.kind == kind, Integration.enabled))
        if _host(i.base_url) == host
    ]
    candidates.sort(key=lambda i: len(i.base_url), reverse=True)
    prefixed = [i for i in candidates if repo_url == i.base_url or repo_url.startswith(i.base_url + "/")]
    return (prefixed or candidates or [None])[0]


async def adopt_legacy_secrets(db: AsyncSession, integration: Integration) -> int:
    """Move Phase 1 tokens of this kind into credentials when it is the only integration of its kind, and link
    projects on its host. Covers installs that upgraded without MWB_*_URL set (migration 0006). Caller commits."""
    same_kind = await db.scalar(
        select(func.count()).select_from(Integration).where(Integration.kind == integration.kind)
    )
    if same_kind != 1:
        return 0
    moved = 0
    for legacy, (ikind, ckind) in LEGACY.items():
        if ikind != integration.kind:
            continue
        for secret in list(await db.scalars(select(UserSecret).where(UserSecret.kind == legacy))):
            exists = await db.scalar(
                select(UserCredential.id).where(
                    UserCredential.user_id == secret.user_id,
                    UserCredential.integration_id == integration.id,
                    UserCredential.kind == ckind,
                )
            )
            if exists is None:
                db.add(
                    UserCredential(
                        user_id=secret.user_id, integration_id=integration.id, kind=ckind, ciphertext=secret.ciphertext
                    )
                )
                moved += 1
        await db.execute(delete(UserSecret).where(UserSecret.kind == legacy))
    if integration.kind in ("gitlab", "gerrit"):
        from app.models import Project

        host = _host(integration.base_url)
        for p in await db.scalars(
            select(Project).where(Project.host == integration.kind, Project.integration_id.is_(None))
        ):
            if _host(p.repo_url) == host:
                p.integration_id = integration.id
    if moved:
        log.info("integrations.adopted_legacy", integration=integration.id, credentials=moved)
    return moved
