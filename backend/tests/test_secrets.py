from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import UserSecret
from app.secrets.service import get_secret


async def test_secrets_are_write_only_and_encrypted(client_for):
    alice = await client_for("alice")
    bob = await client_for("bob")

    assert (await alice.put("/api/secrets/gitlab_token", json={"value": "glpat-secret"})).status_code == 204
    listing = (await alice.get("/api/secrets")).json()
    gitlab = next(s for s in listing if s["kind"] == "gitlab_token")
    assert gitlab["is_set"] is True
    assert "glpat-secret" not in str(listing)

    async with get_sessionmaker()() as db:
        row = await db.scalar(select(UserSecret))
        assert "glpat-secret" not in row.ciphertext
        assert await get_secret(db, alice.user_id, "gitlab_token") == "glpat-secret"
        assert await get_secret(db, bob.user_id, "gitlab_token") is None

    assert (await alice.put("/api/secrets/gitlab_token", json={"value": "rotated"})).status_code == 204
    async with get_sessionmaker()() as db:
        assert await get_secret(db, alice.user_id, "gitlab_token") == "rotated"

    assert not any(s["is_set"] for s in (await bob.get("/api/secrets")).json())
    assert (await alice.delete("/api/secrets/gitlab_token")).status_code == 204
    assert not any(s["is_set"] for s in (await alice.get("/api/secrets")).json())


async def test_unknown_secret_kind(client_for):
    alice = await client_for("alice")
    assert (await alice.put("/api/secrets/aws", json={"value": "x"})).status_code == 404
    assert (await alice.delete("/api/secrets/aws")).status_code == 404
