"""Stored secrets under HashiCorp Vault Transit (envelope encryption), against a fake Vault."""

import httpx2
import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings, get_settings, vault_problems
from app.db import get_sessionmaker
from app.models import SystemState, UserSecret
from app.secrets import service, vault
from tests.fakellm import vault as fake

BASE = {"database_url": "postgresql://x/y", "secret_key": "k", "env": "prod", "hostname": "https://mwb.example.com",
        "cookie_secure": True}  # fmt: skip


@pytest.fixture
def vaulted(monkeypatch):
    s = get_settings()
    legacy = s.fernet_keys[0]
    for key, value in {"secrets_backend": "vault", "vault_addr": "http://vault:8200", "vault_role_id": fake.ROLE_ID,
                       "vault_secret_id": SecretStr(fake.SECRET_ID)}.items():  # fmt: skip
        monkeypatch.setattr(s, key, value)
    fake.reset()
    vault.set_transport(httpx2.ASGITransport(app=fake.app))
    service.forget_keyring()
    yield {"legacy": legacy, "fake": fake}
    service.forget_keyring()
    vault.set_transport(None)


async def test_secrets_are_encrypted_under_a_vault_data_key(make_user, vaulted) -> None:
    with pytest.raises(service.KeyringNotLoaded):  # never silently falls back to the .env key
        service.encrypt("x")
    assert await service.load_keyring() == 1
    async with get_sessionmaker()() as db:
        row = await db.get(SystemState, vault.STATE_KEY)
    [wrapped] = row.value["keys"]
    assert wrapped.startswith("vault:v1:") and fake.STATE["calls"] == ["datakey", "decrypt"]
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await service.set_secret(db, alice.id, "gitlab_token", "glpat-secret")
        await db.commit()
        ct = await db.scalar(select(UserSecret.ciphertext).where(UserSecret.user_id == alice.id))
    with pytest.raises(InvalidToken):  # the database and .env together aren't enough
        Fernet(vaulted["legacy"].encode()).decrypt(ct.encode())
    # A restart: the same data key comes back from Vault, no new one is made.
    service.forget_keyring()
    assert await service.load_keyring() == 1 and fake.STATE["calls"].count("datakey") == 1
    async with get_sessionmaker()() as db:
        assert await service.get_secret(db, alice.id, "gitlab_token") == "glpat-secret"


async def test_switching_to_vault_keeps_old_values_readable_until_rotated(make_user, vaulted, monkeypatch) -> None:
    alice = await make_user("alice")
    old = MultiFernet([Fernet(vaulted["legacy"].encode())]).encrypt(b"glpat-before-vault").decode()
    async with get_sessionmaker()() as db:
        db.add(UserSecret(user_id=alice.id, kind="gitlab_token", ciphertext=old))
        await db.commit()
    await service.load_keyring()
    async with get_sessionmaker()() as db:
        assert await service.get_secret(db, alice.id, "gitlab_token") == "glpat-before-vault"
        counts = await service.rotate_secrets(db)
        await db.commit()
        ct = await db.scalar(select(UserSecret.ciphertext).where(UserSecret.user_id == alice.id))
    assert counts["rotated"] >= 1 and counts["unreadable"] == 0
    monkeypatch.setattr(get_settings(), "secret_keys", "")  # the .env key can now be retired
    monkeypatch.setattr(get_settings(), "secret_key", "")
    assert service.decrypt(ct) == "glpat-before-vault"
    with pytest.raises(InvalidToken):
        Fernet(vaulted["legacy"].encode()).decrypt(ct.encode())


async def test_a_sealed_or_refusing_vault_stops_start_up_with_a_clear_message(vaulted, monkeypatch) -> None:
    fake.STATE["sealed"] = True
    with pytest.raises(vault.VaultError, match="sealed"):
        await service.load_keyring()
    fake.STATE["sealed"] = False
    monkeypatch.setattr(get_settings(), "vault_secret_id", SecretStr("wrong"))
    with pytest.raises(vault.VaultError) as exc:
        await service.load_keyring()
    assert "wrong" not in str(exc.value) and fake.SECRET_ID not in str(exc.value)


async def test_new_data_keys_and_rewrap(make_user, vaulted) -> None:
    await service.load_keyring()
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await service.set_secret(db, alice.id, "jira_token", "jira-1")
        await db.commit()
    assert await service.add_data_key() == 2  # mwb vault new-data-key
    fake.STATE["version"] = 2  # an admin rotated the Transit key in Vault
    assert await service.rewrap_data_keys() == 2  # mwb vault rewrap
    async with get_sessionmaker()() as db:
        keys = (await db.get(SystemState, vault.STATE_KEY)).value["keys"]
    assert all(k.startswith("vault:v2:") for k in keys)
    service.forget_keyring()
    assert await service.load_keyring() == 2
    async with get_sessionmaker()() as db:
        assert await service.get_secret(db, alice.id, "jira_token") == "jira-1"  # written under the old data key
        await service.rotate_secrets(db)
        await db.commit()
        assert await service.get_secret(db, alice.id, "jira_token") == "jira-1"


async def test_the_system_page_reports_vault(make_user, client_for, vaulted) -> None:
    await service.load_keyring()
    await make_user("root", role="admin")
    admin = await client_for("root", create=False)
    block = (await admin.get("/api/admin/system")).json()["secrets"]
    assert block["backend"] == "vault" and block["ok"] and block["data_keys"] == 1
    fake.STATE["sealed"] = True
    assert (await admin.get("/api/admin/system")).json()["secrets"]["error"] == "sealed"


def test_settings_are_checked_at_startup() -> None:
    assert vault_problems(Settings(**BASE)) == []
    assert vault_problems(Settings(**BASE, secrets_backend="vault")) == [
        "Set MWB_VAULT_ADDR, e.g. http://vault:8200 or https://vault.company.example",
        "Set MWB_VAULT_ROLE_ID and MWB_VAULT_SECRET_ID (AppRole), or MWB_VAULT_TOKEN",
    ]
    plain = Settings(**BASE, secrets_backend="vault", vault_addr="http://vault.company.example", vault_token="t")
    assert vault_problems(plain) == ["MWB_VAULT_ADDR must be https (plain http only to the compose service vault)"]
    ours = Settings(**BASE, secrets_backend="vault", vault_addr="http://vault:8200", vault_token="t")
    assert vault_problems(ours) == []
