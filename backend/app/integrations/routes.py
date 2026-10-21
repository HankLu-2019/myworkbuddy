"""Integrations API (design 02 §7): admins manage integrations; users manage their own write-only credentials."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.audit.service import audit
from app.deps import DB, AdminAuth, CurrentAuth, client_ip
from app.errors import AppError
from app.integrations.kinds import CREDENTIALS, HELP, validate_base_url, validate_ca_bundle, validate_settings
from app.integrations.service import (
    adopt_legacy_secrets,
    credential_rows,
    get_credentials,
    record_verification,
    set_credential,
    state_of,
)
from app.integrations.verify import verify
from app.models import Integration, Project, UserCredential

router = APIRouter(prefix="/api", tags=["integrations"])

Kind = Literal["jira", "gitlab", "gerrit", "jenkins", "kubernetes", "feed"]
CredState = Literal["not_set", "unverified", "ok", "invalid", "unreadable"]


class CredentialOut(BaseModel):
    kind: str
    label: str
    status: CredState
    verified_at: datetime | None
    last_error: str | None
    updated_at: datetime | None


class IntegrationOut(BaseModel):
    id: int
    kind: Kind
    name: str
    base_url: str
    help: str
    credentials: list[CredentialOut]


class CredentialIn(BaseModel):
    value: str = Field(min_length=1, max_length=70_000)


class TestOut(BaseModel):
    ok: bool
    status: CredState
    message: str


class AdminIntegrationOut(BaseModel):
    id: int
    kind: Kind
    name: str
    base_url: str
    settings: dict[str, Any]
    has_ca_bundle: bool
    enabled: bool
    credentials: int  # how many users saved credentials
    projects: int
    updated_at: datetime


class IntegrationCreate(BaseModel):
    kind: Kind
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=512)
    settings: dict[str, Any] = {}
    ca_bundle: str | None = Field(default=None, max_length=70_000)
    enabled: bool = True


class IntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    settings: dict[str, Any] | None = None
    ca_bundle: str | None = Field(default=None, max_length=70_000)  # "" removes it
    enabled: bool | None = None


async def _enabled(db: DB, integration_id: int) -> Integration:
    integration = await db.get(Integration, integration_id)
    if integration is None or not integration.enabled:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Integration not found")
    return integration


async def _out(db: DB, integration: Integration, user_id: int) -> IntegrationOut:
    rows = await credential_rows(db, user_id, integration.id)
    return IntegrationOut(
        id=integration.id,
        kind=integration.kind,
        name=integration.name,
        base_url=integration.base_url,
        help=HELP.get(integration.kind, ""),
        credentials=[
            CredentialOut(
                kind=k,
                label=label,
                status=state_of(rows.get(k)),
                verified_at=rows[k].verified_at if k in rows else None,
                last_error=rows[k].last_error if k in rows else None,
                updated_at=rows[k].updated_at if k in rows else None,
            )
            for k, label in CREDENTIALS.get(integration.kind, {}).items()
        ],
    )


# ---------- users ----------


@router.get("/integrations", response_model=list[IntegrationOut])
async def list_integrations(auth: CurrentAuth, db: DB) -> list[IntegrationOut]:
    rows = await db.scalars(select(Integration).where(Integration.enabled).order_by(Integration.kind, Integration.name))
    return [await _out(db, i, auth.user.id) for i in rows]


@router.put("/integrations/{integration_id}/credentials/{kind}", status_code=204)
async def put_credential(integration_id: int, kind: str, body: CredentialIn, auth: CurrentAuth, db: DB) -> None:
    integration = await _enabled(db, integration_id)
    await set_credential(db, auth.user.id, integration, kind, body.value)
    await audit(
        db,
        "credential.set",
        user_id=auth.user.id,
        target_type="integration",
        target_id=integration.id,
        detail={"kind": kind},
    )
    await db.commit()


@router.delete("/integrations/{integration_id}/credentials/{kind}", status_code=204)
async def delete_credential(integration_id: int, kind: str, auth: CurrentAuth, db: DB) -> None:
    integration = await _enabled(db, integration_id)
    await db.execute(
        delete(UserCredential).where(
            UserCredential.user_id == auth.user.id,
            UserCredential.integration_id == integration.id,
            UserCredential.kind == kind,
        )
    )
    await audit(
        db,
        "credential.delete",
        user_id=auth.user.id,
        target_type="integration",
        target_id=integration.id,
        detail={"kind": kind},
    )
    await db.commit()


@router.post("/integrations/{integration_id}/test", response_model=TestOut)
async def test_credential(integration_id: int, auth: CurrentAuth, db: DB) -> TestOut:
    """Run the kind's verify call with the caller's credential and record the result (02 §1)."""
    user_id = auth.user.id
    integration = await _enabled(db, integration_id)
    try:
        creds = await get_credentials(db, user_id, integration)
    except AppError as exc:
        return TestOut(ok=False, status="unreadable", message=str(exc.detail))
    if integration.kind != "feed" and not creds:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"Save your {integration.name} credentials first.")
    db.expunge(integration)
    await db.commit()  # rule R1: the verify call below goes to the network
    result = await verify(integration, creds)
    new = await record_verification(
        db, user_id, integration_id, ok=result.ok, auth_failed=result.auth_failed, message=result.message
    )
    await audit(
        db,
        "credential.test",
        user_id=user_id,
        target_type="integration",
        target_id=integration_id,
        detail={"ok": result.ok},
    )
    await db.commit()
    return TestOut(ok=result.ok, status=new, message=result.message)


# ---------- admins ----------


async def _admin_out(db: DB, i: Integration) -> AdminIntegrationOut:
    creds = await db.scalar(
        select(func.count(func.distinct(UserCredential.user_id))).where(UserCredential.integration_id == i.id)
    )
    projects = await db.scalar(select(func.count()).select_from(Project).where(Project.integration_id == i.id))
    return AdminIntegrationOut(
        id=i.id, kind=i.kind, name=i.name, base_url=i.base_url, settings=i.settings, has_ca_bundle=bool(i.ca_bundle),
        enabled=i.enabled, credentials=creds or 0, projects=projects or 0, updated_at=i.updated_at,
    )  # fmt: skip


async def _unique_name(db: DB, name: str, exclude: int | None = None) -> None:
    q = select(Integration.id).where(func.lower(Integration.name) == name.strip().lower())
    if exclude is not None:
        q = q.where(Integration.id != exclude)
    if await db.scalar(q):
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"An integration called {name!r} already exists.")


@router.get("/admin/integrations", response_model=list[AdminIntegrationOut])
async def admin_list(auth: AdminAuth, db: DB) -> list[AdminIntegrationOut]:
    return [
        await _admin_out(db, i)
        for i in await db.scalars(select(Integration).order_by(Integration.kind, Integration.name))
    ]


@router.post("/admin/integrations", response_model=AdminIntegrationOut, status_code=201)
async def admin_create(body: IntegrationCreate, auth: AdminAuth, db: DB, request: Request) -> AdminIntegrationOut:
    await _unique_name(db, body.name)
    integration = Integration(
        kind=body.kind,
        name=body.name.strip(),
        base_url=validate_base_url(body.base_url),
        settings=validate_settings(body.kind, body.settings),
        ca_bundle=validate_ca_bundle(body.ca_bundle),
        enabled=body.enabled,
    )
    db.add(integration)
    await db.flush()
    adopted = await adopt_legacy_secrets(db, integration)
    await audit(
        db, "integration.create", user_id=auth.user.id, target_type="integration", target_id=integration.id,
        detail={"kind": body.kind, "adopted": adopted}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return await _admin_out(db, integration)


@router.patch("/admin/integrations/{integration_id}", response_model=AdminIntegrationOut)
async def admin_update(
    integration_id: int, body: IntegrationUpdate, auth: AdminAuth, db: DB, request: Request
) -> AdminIntegrationOut:
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Integration not found")
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed and body.name:
        await _unique_name(db, body.name, exclude=integration.id)
        integration.name = body.name.strip()
    if "base_url" in changed and body.base_url:
        new_url = validate_base_url(body.base_url)
        if new_url != integration.base_url:
            # Credentials were checked against the old address; kubeconfigs name it explicitly.
            for row in await db.scalars(select(UserCredential).where(UserCredential.integration_id == integration.id)):
                row.status = "unverified"
            if integration.kind == "kubernetes":
                await db.execute(delete(UserCredential).where(UserCredential.integration_id == integration.id))
        integration.base_url = new_url
    if "settings" in changed:
        integration.settings = validate_settings(integration.kind, body.settings)
    if "ca_bundle" in changed:
        integration.ca_bundle = validate_ca_bundle(body.ca_bundle)
    if "enabled" in changed and body.enabled is not None:
        integration.enabled = body.enabled
    await audit(
        db, "integration.update", user_id=auth.user.id, target_type="integration", target_id=integration.id,
        detail={"fields": sorted(changed)}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return await _admin_out(db, integration)


@router.delete("/admin/integrations/{integration_id}", status_code=204)
async def admin_delete(integration_id: int, auth: AdminAuth, db: DB, request: Request) -> None:
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Integration not found")
    used = await db.scalar(select(func.count()).select_from(Project).where(Project.integration_id == integration.id))
    if used:
        raise AppError(
            status.HTTP_409_CONFLICT, "conflict",
            f"{used} repositories are indexed through {integration.name}; disable it instead, or remove them first.",
        )  # fmt: skip
    await db.delete(integration)  # credentials cascade
    await audit(
        db,
        "integration.delete",
        user_id=auth.user.id,
        target_type="integration",
        target_id=integration_id,
        ip=client_ip(request),
    )
    await db.commit()
