"""Design 01 §H6: indexing reuses vectors, ingest commits per file, the DB ends idle transactions."""

from sqlalchemy import select, text

from app.db import get_sessionmaker
from app.knowledge.chunking import ChunkIn
from app.knowledge.indexer import upsert_document
from app.knowledge.ingest import repos
from app.knowledge.ingest.repos import ingest_project
from app.llm.embeddings import HashEmbedder, set_embedder
from app.llm.models import set_model_override
from app.models import Document, Project
from tests import txguard
from tests.helpers import scripted_model
from tests.test_ingest import _project, fixture_repo  # noqa: F401  (pytest fixture)


class CountingEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.inner = HashEmbedder()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        txguard.check("embedding")
        self.texts.extend(texts)
        return await self.inner.embed(texts)


async def test_unchanged_chunks_reuse_their_vectors(make_user) -> None:
    alice = await make_user("alice")
    counter = CountingEmbedder()
    set_embedder(counter)
    kw = {"owner_id": alice.id, "source_type": "upload", "source_ref": "upload:x", "title": "T"}
    async with get_sessionmaker()() as db:
        await upsert_document(db, **kw, chunks=[ChunkIn("alpha"), ChunkIn("beta")])
        await db.commit()
        assert len(counter.texts) == 2
        await upsert_document(db, **kw, chunks=[ChunkIn("alpha"), ChunkIn("beta")])
        await db.commit()
        assert len(counter.texts) == 2  # identical document: no embedding call at all
        await upsert_document(db, **kw, chunks=[ChunkIn("alpha"), ChunkIn("gamma")])
        await db.commit()
    assert len(counter.texts) == 3 and counter.texts[-1].endswith("gamma")  # only the changed chunk


async def test_ingest_keeps_other_files_when_one_fails(client_for, fixture_repo, monkeypatch) -> None:  # noqa: F811
    alice = await client_for("alice")
    for i in range(4):
        (fixture_repo / "src" / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    import subprocess

    subprocess.run(["git", "add", "."], cwd=fixture_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=a", "-c", "user.email=a@x", "commit", "-qm", "more"],
        cwd=fixture_repo,
        check=True,
        capture_output=True,
    )

    async def fake_checkout(db, project):
        return fixture_repo

    real = repos.upsert_document

    async def flaky(db, **kw):
        if kw["source_ref"].endswith("src/mod2.py"):
            raise RuntimeError("disk full")
        return await real(db, **kw)

    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    monkeypatch.setattr(repos, "upsert_document", flaky)
    set_model_override(scripted_model("# pipeline\n\ncard", structured={}))
    pid = await _project(alice.user_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)

    async with get_sessionmaker()() as db:
        project = await db.get(Project, pid)
        refs = set(await db.scalars(select(Document.source_ref).where(Document.project_id == pid)))
    assert project.status == "ready"
    assert "src/mod2.py" in (project.error or "")
    assert {f"{pid}:src/mod{i}.py" for i in (0, 1, 3)} <= refs
    assert f"{pid}:src/mod2.py" not in refs


async def test_idle_in_transaction_timeout_is_set() -> None:
    async with get_sessionmaker()() as db:
        value = await db.scalar(text("SHOW idle_in_transaction_session_timeout"))
    assert value == "1min"


async def test_guard_catches_a_network_call_inside_a_transaction(make_user) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await db.execute(text("SELECT 1"))  # transaction now open
        txguard.check("embedding")
        assert txguard.violations and "rule R1" in txguard.violations[0]
        await db.rollback()
    txguard.reset()
    assert alice.id


async def test_guard_sees_a_parent_transaction_but_not_a_sibling_one(make_user) -> None:
    import asyncio

    alice = await make_user("alice")
    started, release = asyncio.Event(), asyncio.Event()

    async def sibling_write() -> None:  # e.g. a progress update in its own short transaction
        async with get_sessionmaker()() as db:
            await db.execute(text("SELECT 1"))
            started.set()
            await release.wait()
            await db.commit()

    async def call() -> None:
        await started.wait()
        txguard.check("LLM request")
        release.set()

    await asyncio.gather(sibling_write(), call())
    assert txguard.violations == []

    async with get_sessionmaker()() as db:
        await db.execute(text("SELECT 1"))  # open in the parent, then fanned out
        await asyncio.gather(asyncio.create_task(asyncio.sleep(0)), call_check())
        await db.rollback()
    assert txguard.violations and "rule R1" in txguard.violations[0]
    txguard.reset()
    assert alice.id


async def call_check() -> None:
    txguard.check("LLM request")
