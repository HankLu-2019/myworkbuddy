from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.models import UserSecret

SECRET_KINDS = {
    "gitlab_token": "GitLab personal access token",
    "gerrit_username": "Gerrit username",
    "gerrit_password": "Gerrit HTTP password",
    "jira_token": "Jira personal access token",
}


def _fernet() -> MultiFernet:
    # First key encrypts; every key decrypts, so keys can be rotated (06-operations §8).
    return MultiFernet([Fernet(k.encode()) for k in get_settings().fernet_keys])


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    """The plaintext, or None when no configured key can read it."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


async def set_secret(db: AsyncSession, user_id: int, kind: str, value: str) -> None:
    ciphertext = _fernet().encrypt(value.encode()).decode()
    row = await db.scalar(select(UserSecret).where(UserSecret.user_id == user_id, UserSecret.kind == kind))
    if row is None:
        db.add(UserSecret(user_id=user_id, kind=kind, ciphertext=ciphertext))
    else:
        row.ciphertext = ciphertext


def readable(ciphertext: str) -> bool:
    try:
        _fernet().decrypt(ciphertext.encode())
        return True
    except InvalidToken:
        return False


async def get_secret(db: AsyncSession, user_id: int, kind: str) -> str | None:
    """The decrypted value, or None if unset. A value no configured key can decrypt (e.g. the key it was
    encrypted with was removed from MWB_SECRET_KEYS) raises 409 credential_invalid instead of a 500."""
    row = await db.scalar(select(UserSecret).where(UserSecret.user_id == user_id, UserSecret.kind == kind))
    if row is None:
        return None
    try:
        return _fernet().decrypt(row.ciphertext.encode()).decode()
    except InvalidToken:
        label = SECRET_KINDS.get(kind, kind)
        raise AppError(
            status.HTTP_409_CONFLICT, "credential_invalid", f"Your {label} can't be read. Re-enter it in Settings."
        ) from None


async def rotate_secrets(db: AsyncSession) -> dict[str, int]:
    """Re-encrypt every secret with the first key in MWB_SECRET_KEYS. The caller commits.

    Rows no key can decrypt are left as they are and counted, so the user can re-enter them.
    """
    from app.models import UserCredential

    f = _fernet()
    counts = {"rotated": 0, "unreadable": 0}
    for model in (UserSecret, UserCredential):
        for row in await db.scalars(select(model).order_by(model.id).with_for_update()):
            try:
                row.ciphertext = f.rotate(row.ciphertext.encode()).decode()
                counts["rotated"] += 1
            except InvalidToken:
                counts["unreadable"] += 1
    return counts
