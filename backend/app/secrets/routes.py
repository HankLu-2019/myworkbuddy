from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.audit.service import audit
from app.deps import DB, CurrentAuth
from app.models import UserSecret
from app.secrets.service import SECRET_KINDS, set_secret

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


class SecretStatus(BaseModel):
    kind: str
    label: str
    is_set: bool
    updated_at: datetime | None


class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


def _check_kind(kind: str) -> None:
    if kind not in SECRET_KINDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown secret kind")


@router.get("", response_model=list[SecretStatus])
async def list_secrets(auth: CurrentAuth, db: DB) -> list[SecretStatus]:
    rows = {r.kind: r for r in await db.scalars(select(UserSecret).where(UserSecret.user_id == auth.user.id))}
    return [
        SecretStatus(kind=k, label=label, is_set=k in rows, updated_at=rows[k].updated_at if k in rows else None)
        for k, label in SECRET_KINDS.items()
    ]


@router.put("/{kind}", status_code=204)
async def put_secret(kind: str, body: SecretIn, auth: CurrentAuth, db: DB) -> None:
    _check_kind(kind)
    await set_secret(db, auth.user.id, kind, body.value)
    await audit(db, "secret.set", user_id=auth.user.id, target_type="secret", target_id=kind)
    await db.commit()


@router.delete("/{kind}", status_code=204)
async def delete_secret(kind: str, auth: CurrentAuth, db: DB) -> None:
    _check_kind(kind)
    await db.execute(delete(UserSecret).where(UserSecret.user_id == auth.user.id, UserSecret.kind == kind))
    await audit(db, "secret.delete", user_id=auth.user.id, target_type="secret", target_id=kind)
    await db.commit()
