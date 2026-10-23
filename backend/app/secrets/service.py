from datetime import UTC, datetime

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


# Data keys unwrapped by Vault (MWB_SECRETS_BACKEND=vault), newest first; None until load_keyring() ran.
_vault_keys: list[str] | None = None


class KeyringNotLoaded(RuntimeError):
    pass


def _fernet() -> MultiFernet:
    """First key encrypts; every key decrypts, so keys can be rotated (06-operations §8). With Vault, its data keys
    come first, so new values are encrypted under Vault's key; MWB_SECRET_KEYS, when still set, only reads older
    values until `mwb rotate-secrets` has moved them."""
    s = get_settings()
    keys: list[str] = []
    if s.secrets_backend == "vault":
        if _vault_keys is None:
            raise KeyringNotLoaded("The Vault data keys aren't loaded; the process must call load_keyring() at start")
        keys += _vault_keys
    keys += s.fernet_keys
    return MultiFernet([Fernet(k.encode()) for k in keys])


async def load_keyring(*, create: bool = True) -> int:
    """With MWB_SECRETS_BACKEND=vault: read the wrapped data keys, make the first one if there is none, and have
    Vault unwrap them. Uses its own short-lived engine (the CLI calls it from a separate event loop), and holds no
    transaction while Vault is called (rule R1). Returns how many data keys are loaded; 0 with Fernet."""
    global _vault_keys
    s = get_settings()
    if s.secrets_backend != "vault":
        return 0
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models import SystemState
    from app.secrets import vault

    engine = create_async_engine(s.async_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(select(SystemState.value).where(SystemState.key == vault.STATE_KEY))).scalar()
            await conn.commit()
        wrapped = list((row or {}).get("keys") or [])
        if not wrapped:
            if not create:
                raise vault.VaultError("No Vault data key yet: start the api once to create it")
            _key, ct = await vault.new_data_key()  # network: nothing open
            value = {"keys": [ct], "created_at": datetime.now(UTC).isoformat()}
            async with engine.begin() as conn:  # another process may have won the race: keep theirs
                await conn.execute(insert(SystemState).values(key=vault.STATE_KEY, value=value)
                                   .on_conflict_do_nothing(index_elements=["key"]))  # fmt: skip
            async with engine.connect() as conn:
                row = (await conn.execute(select(SystemState.value).where(SystemState.key == vault.STATE_KEY))).scalar()
                await conn.commit()
            wrapped = list(row["keys"])
        _vault_keys = await vault.unwrap(wrapped)  # network: nothing open
    finally:
        await engine.dispose()
    return len(_vault_keys)


async def _wrapped_keys() -> list[str]:
    from app.db import get_sessionmaker
    from app.models import SystemState
    from app.secrets import vault

    async with get_sessionmaker()() as db:
        row = await db.get(SystemState, vault.STATE_KEY)
        await db.commit()
        return list((row.value if row else {}).get("keys") or [])


async def _save_wrapped(keys: list[str]) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from app.db import get_sessionmaker
    from app.models import SystemState
    from app.secrets import vault

    value = {"keys": keys, "updated_at": datetime.now(UTC).isoformat()}
    async with get_sessionmaker()() as db:
        await db.execute(insert(SystemState).values(key=vault.STATE_KEY, value=value)
                         .on_conflict_do_update(index_elements=["key"], set_={"value": value}))  # fmt: skip
        await db.commit()


async def add_data_key() -> int:
    """`mwb vault new-data-key`: a new data key first (it encrypts from now on), the older ones kept to decrypt."""
    from app.secrets import vault

    _key, ct = await vault.new_data_key()  # network: nothing open
    keys = [ct, *await _wrapped_keys()]
    await _save_wrapped(keys)
    return len(keys)


async def rewrap_data_keys() -> int:
    """`mwb vault rewrap`: after the Transit key was rotated in Vault, wrap the data keys with its newest version."""
    from app.secrets import vault

    keys = await vault.rewrap(await _wrapped_keys())  # network: nothing open
    await _save_wrapped(keys)
    return len(keys)


def forget_keyring() -> None:
    """Tests, and `mwb vault` commands that reload."""
    global _vault_keys
    _vault_keys = None


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
    """Re-encrypt every secret with the first key (the newest Vault data key, or the first in MWB_SECRET_KEYS). The
    caller commits.

    Rows no key can decrypt are left as they are and counted, so the user can re-enter them.
    """
    from app.models import JobSecret, McpServer, NotifyChannel, UserCredential

    f = _fernet()
    counts = {"rotated": 0, "unreadable": 0}
    # Every encrypted column: user tokens, integration credentials, job keys, chat webhooks, MCP credentials.
    columns = [(UserSecret, "ciphertext"), (UserCredential, "ciphertext"), (JobSecret, "ciphertext"),
               (NotifyChannel, "url_enc"), (McpServer, "auth_value_enc")]  # fmt: skip
    for model, attr in columns:
        for row in await db.scalars(select(model).with_for_update()):
            value = getattr(row, attr)
            if not value:
                continue
            try:
                setattr(row, attr, f.rotate(value.encode()).decode())
                counts["rotated"] += 1
            except InvalidToken:
                counts["unreadable"] += 1
    return counts
