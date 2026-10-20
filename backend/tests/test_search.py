from sqlalchemy import func, select

from app.db import get_sessionmaker
from app.knowledge.chunking import ChunkIn
from app.knowledge.indexer import embed_missing, upsert_document
from app.knowledge.search import hybrid_search
from app.llm.embeddings import HashEmbedder, set_embedder
from app.models import Chunk, User


class BrokenEmbedder:
    async def embed(self, texts):
        raise RuntimeError("gateway down")


async def _seed(owner: User):
    async with get_sessionmaker()() as db:
        for ref, title, body in [
            ("a", "Spark OOM", "Executor OOMKilled in job SPIN-4412 after the shuffle grew"),
            ("b", "Deploy hang", "Spinnaker bake stage hangs when the jenkins trigger times out"),
            ("c", "Unrelated", "Lunch menu for the offsite"),
        ]:
            await upsert_document(
                db, owner_id=owner.id, source_type="upload", source_ref=ref, title=title, chunks=[ChunkIn(body)]
            )
        await db.commit()


async def test_exact_identifier_ranks_first(make_user):
    alice = await make_user("alice")
    await _seed(alice)
    async with get_sessionmaker()() as db:
        hits = await hybrid_search(db, alice, "SPIN-4412")
    assert hits[0].title == "Spark OOM"
    assert hits[0].score > 0


async def test_filters_and_empty_query(make_user):
    alice = await make_user("alice")
    await _seed(alice)
    async with get_sessionmaker()() as db:
        assert await hybrid_search(db, alice, "   ") == []
        assert await hybrid_search(db, alice, "spinnaker", source_types=["wiki"]) == []
        assert await hybrid_search(db, alice, "spinnaker", project_id=42) == []
        hits = await hybrid_search(db, alice, "spinnaker jenkins", source_types=["upload"], limit=1)
    assert [h.title for h in hits] == ["Deploy hang"]


async def test_reindex_only_when_content_changes(make_user):
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        doc = await upsert_document(
            db,
            owner_id=alice.id,
            source_type="upload",
            source_ref="x",
            title="T",
            chunks=[ChunkIn("one"), ChunkIn("two")],
        )
        first = list(await db.scalars(select(Chunk.id).where(Chunk.document_id == doc.id)))
        await upsert_document(
            db,
            owner_id=alice.id,
            source_type="upload",
            source_ref="x",
            title="T",
            chunks=[ChunkIn("one"), ChunkIn("two")],
        )
        assert list(await db.scalars(select(Chunk.id).where(Chunk.document_id == doc.id))) == first
        await upsert_document(
            db, owner_id=alice.id, source_type="upload", source_ref="x", title="T", chunks=[ChunkIn("three")]
        )
        assert list(await db.scalars(select(Chunk.content).where(Chunk.document_id == doc.id))) == ["three"]


async def test_embedding_outage_falls_back_to_bm25_and_backfills(make_user):
    alice = await make_user("alice")
    set_embedder(BrokenEmbedder())
    await _seed(alice)
    async with get_sessionmaker()() as db:
        missing = await db.scalar(select(func.count()).where(Chunk.embedding.is_(None)))
        assert missing == 3
        hits = await hybrid_search(db, alice, "spinnaker")
        assert [h.title for h in hits] == ["Deploy hang"]

    set_embedder(HashEmbedder())
    async with get_sessionmaker()() as db:
        assert await embed_missing(db) == 3
        await db.commit()
        assert await embed_missing(db) == 0
        assert await db.scalar(select(func.count()).where(Chunk.embedding.is_(None))) == 0
