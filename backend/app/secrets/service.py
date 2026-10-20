from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import UserSecret

SECRET_KINDS = {
    "gitlab_token": "GitLab personal access token",
    "gerrit_username": "Gerrit username",
    "gerrit_password": "Gerrit HTTP password",
    "jira_token": "Jira personal access token",
}


def _fernet() -> Fernet:
    return Fernet(get_settings().secret_key.encode())


async def set_secret(db: AsyncSession, user_id: int, kind: str, value: str) -> None:
    ciphertext = _fernet().encrypt(value.encode()).decode()
    row = await db.scalar(select(UserSecret).where(UserSecret.user_id == user_id, UserSecret.kind == kind))
    if row is None:
        db.add(UserSecret(user_id=user_id, kind=kind, ciphertext=ciphertext))
    else:
        row.ciphertext = ciphertext


async def get_secret(db: AsyncSession, user_id: int, kind: str) -> str | None:
    row = await db.scalar(select(UserSecret).where(UserSecret.user_id == user_id, UserSecret.kind == kind))
    return _fernet().decrypt(row.ciphertext.encode()).decode() if row else None
