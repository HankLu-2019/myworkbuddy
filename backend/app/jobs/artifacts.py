"""Job artifacts on disk (design 04 §2): /data/artifacts/<owner_id>/<job_id>/<name>, with a row each (size,
sha256). Downloads check ownership (04 §8)."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Artifact


def root() -> Path:
    return get_settings().data_dir / "artifacts"


def path_of(artifact: Artifact) -> Path:
    return root() / artifact.key


async def store(db: AsyncSession, owner_id: int, job_id: uuid.UUID, name: str, data: bytes, mime: str) -> Artifact:
    key = f"{owner_id}/{job_id}/{name}"
    target = root() / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    row = Artifact(
        owner_id=owner_id, job_id=job_id, key=key, mime=mime, size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), preview=data[:500].decode(errors="replace"),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row
