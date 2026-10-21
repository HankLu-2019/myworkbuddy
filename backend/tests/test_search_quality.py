"""Search quality (design 01 §H10): multilingual BM25, embedding provenance, reembed, poison chunks."""

import httpx
import pytest
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from app.db import get_sessionmaker
from app.knowledge.chunking import ChunkIn
from app.knowledge.indexer import MAX_EMBED_ATTEMPTS, embed_missing, reembed, upsert_document
from app.knowledge.search import chinese_query, hybrid_search
from app.llm import embeddings
from app.llm.embeddings import EmbedModelChanged, GatewayEmbedder, HashEmbedder, is_permanent_failure, set_embedder
from app.models import Chunk, User

DOCS = [
    ("zh", "部署记录", "部署失败，镜像拉取超时"),
    ("zh2", "上线", "部署成功，服务已上线"),
    ("sv", "Incident", "Driftsättningen misslyckades på grund av en timeout i databasen"),
    ("mixed", "构建", "构建失败: ImagePullBackOff on node 3"),
    ("en", "Spark OOM", "Executor OOMKilled in job SPIN-4412 after the shuffle grew"),
    ("en2", "Lunch", "the quick brown fox and another english line with spaces"),
    ("zh3", "迁移", "镜像仓库迁移计划"),
]


class NoVectors:
    """Keyword search only, so these tests measure BM25 rather than the hash embedder."""

    model_id = "none"

    async def embed(self, texts):
        raise httpx.ConnectError("down")


async def _seed(owner: User) -> None:
    async with get_sessionmaker()() as db:
        for ref, title, body in DOCS:
            await upsert_document(
                db, owner_id=owner.id, source_type="upload", source_ref=ref, title=title, chunks=[ChunkIn(body)]
            )
        await db.commit()


async def _refs(user: User, q: str) -> list[str]:
    async with get_sessionmaker()() as db:
        return [h.source_ref for h in await hybrid_search(db, user, q, limit=10)]


@pytest.fixture
async def seeded(make_user) -> User:
    set_embedder(NoVectors())
    alice = await make_user("alice")
    await _seed(alice)
    return alice


async def test_english_identifiers_rank_first(seeded) -> None:
    assert (await _refs(seeded, "SPIN-4412"))[0] == "en"
    assert (await _refs(seeded, "OOMKilled"))[0] == "en"
    # A multi-word English query does not match everything through the Chinese field.
    assert await _refs(seeded, "OOMKilled SPIN-4412") == ["en"]


async def test_chinese_word_is_found(seeded) -> None:
    refs = await _refs(seeded, "部署失败")
    assert "zh" in refs[:3] and refs[0] == "zh"
    assert set((await _refs(seeded, "镜像"))[:2]) == {"zh", "zh3"}


async def test_swedish_word_is_found(seeded) -> None:
    assert await _refs(seeded, "driftsättningen") == ["sv"]
    assert await _refs(seeded, "Driftsättningen") == ["sv"]  # lowercased


async def test_mixed_chinese_and_english(seeded) -> None:
    assert (await _refs(seeded, "ImagePullBackOff"))[0] == "mixed"
    assert (await _refs(seeded, "构建失败"))[0] == "mixed"
    assert (await _refs(seeded, "构建失败 ImagePullBackOff"))[0] == "mixed"


def test_chinese_query_strips_spaces_and_punctuation() -> None:
    assert chinese_query("OOMKilled SPIN-4412") is None
    assert chinese_query("构建失败， ImagePullBackOff!") == "构建失败ImagePullBackOff"
    assert chinese_query("部署 失败") == "部署失败"


# ---------- embedding provenance ----------


class OtherModel(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.model_id = "other-model"


async def test_vectors_from_another_model_are_ignored(make_user) -> None:
    alice = await make_user("alice")
    set_embedder(HashEmbedder())
    async with get_sessionmaker()() as db:
        await upsert_document(
            db, owner_id=alice.id, source_type="upload", source_ref="a", title="Bake", chunks=[ChunkIn("bake stage")]
        )
        await db.commit()
        assert set(await db.scalars(select(Chunk.embed_model))) == {"hash-1024"}
        hits = await hybrid_search(db, alice, "bake stage")
        assert len(hits) == 1 and abs(hits[0].score - 2 / 61) < 1e-9  # ranked first by both lists (RRF)

    set_embedder(OtherModel())
    async with get_sessionmaker()() as db:
        hits = await hybrid_search(db, alice, "bake stage")
        assert len(hits) == 1 and abs(hits[0].score - 1 / 61) < 1e-9  # keyword list only


async def test_reindex_reuses_only_vectors_of_the_current_model(make_user) -> None:
    alice = await make_user("alice")
    calls: list[int] = []

    class Counting(HashEmbedder):
        async def embed(self, texts):
            calls.append(len(texts))
            return await super().embed(texts)

    set_embedder(Counting())
    kw = {"owner_id": alice.id, "source_type": "upload", "source_ref": "x", "title": "T"}
    async with get_sessionmaker()() as db:
        await upsert_document(db, **kw, chunks=[ChunkIn("one"), ChunkIn("two")])
        await db.commit()
        await upsert_document(db, **kw, chunks=[ChunkIn("one"), ChunkIn("two"), ChunkIn("three")])
        await db.commit()
        assert calls == [2, 1]  # unchanged chunks reuse their vectors
        await db.execute(update(Chunk).values(embed_model="old-model"))
        await db.commit()
        await upsert_document(db, **kw, chunks=[ChunkIn("one"), ChunkIn("two"), ChunkIn("four")])
        await db.commit()
        assert calls == [2, 1, 3]  # vectors of another model are not reused
        assert set(await db.scalars(select(Chunk.embed_model))) == {"hash-1024"}


async def test_gateway_records_the_resolved_model_and_refuses_a_switch(monkeypatch) -> None:
    models = iter(["amazon.titan-embed-text-v2:0", "amazon.titan-embed-text-v2:0", "cohere.embed-v4"])

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(__import__("json").loads(request.content)["input"])
        return httpx.Response(
            200, json={"model": next(models), "data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    e = GatewayEmbedder()
    assert e.model_id == e._model  # configured alias until the first call
    assert await e.resolve_model() == "amazon.titan-embed-text-v2:0"
    assert len(await e.embed(["a", "b"])) == 2
    with pytest.raises(EmbedModelChanged):
        await e.embed(["c"])


def test_permanent_failures_are_input_errors_only() -> None:
    req = httpx.Request("POST", "http://g/v1/embeddings")

    def status(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))

    assert is_permanent_failure(status(400)) and is_permanent_failure(status(413))
    for code in (401, 429, 500, 503):
        assert not is_permanent_failure(status(code))
    assert not is_permanent_failure(httpx.ConnectError("down"))
    assert not is_permanent_failure(EmbedModelChanged("x"))
    assert is_permanent_failure(ValueError("bad response"))


# ---------- reembed ----------


async def test_reembed_dry_run_reports_and_changes_nothing(make_user) -> None:
    alice = await make_user("alice")
    set_embedder(HashEmbedder())
    async with get_sessionmaker()() as db:
        for ref in ("a", "b", "c"):
            await upsert_document(
                db, owner_id=alice.id, source_type="upload", source_ref=ref, title=ref, chunks=[ChunkIn(f"text {ref}")]
            )
        await db.commit()
        await db.execute(update(Chunk).where(Chunk.id.in_(select(Chunk.id).limit(2))).values(embed_model="old"))
        await db.commit()

        report = await reembed(db, dry_run=True)
        assert report == {
            "current_model": "hash-1024",
            "other_models": {"old": 2},
            "missing": 0,
            "failed": 0,
            "cleared": 0,
        }
        assert await db.scalar(select(func.count()).where(Chunk.embedding.is_(None))) == 0

        report = await reembed(db, dry_run=False)
        assert report["cleared"] == 2
        assert await db.scalar(select(func.count()).where(Chunk.embedding.is_(None))) == 2
        assert await embed_missing(db) == 2
        await db.commit()
        assert set(await db.scalars(select(Chunk.embed_model))) == {"hash-1024"}
        assert (await reembed(db, dry_run=True))["other_models"] == {}


def test_reembed_cli_prints_the_report(monkeypatch) -> None:
    from app import cli
    from app.knowledge import indexer

    async def fake(db, *, dry_run):
        return {"current_model": "m", "other_models": {"old": 4}, "missing": 1, "failed": 2, "cleared": 0}

    monkeypatch.setattr(indexer, "reembed", fake)
    result = CliRunner().invoke(cli.cli, ["reembed", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "current model: m" in result.output and "other model old: 4 chunks" in result.output
    assert "cleared: 0 (dry run)" in result.output


# ---------- poison chunks ----------


class RejectsPoison(HashEmbedder):
    """Like a gateway that refuses one input (e.g. too many tokens) with 400; a batch containing it fails."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if any("POISON" in t for t in texts):
            req = httpx.Request("POST", "http://g")
            raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))
        return await super().embed(texts)


async def test_poison_chunk_stops_after_five_attempts_and_does_not_block_others(make_user) -> None:
    alice = await make_user("alice")
    set_embedder(NoVectors())
    async with get_sessionmaker()() as db:
        await upsert_document(
            db,
            owner_id=alice.id,
            source_type="upload",
            source_ref="p",
            title="P",
            chunks=[ChunkIn("good one"), ChunkIn("POISON chunk"), ChunkIn("good two")],
        )
        await db.commit()

    emb = RejectsPoison()
    set_embedder(emb)
    async with get_sessionmaker()() as db:
        assert await embed_missing(db) == 2  # the good chunks get vectors on the first tick
        await db.commit()
        for _ in range(MAX_EMBED_ATTEMPTS - 1):
            assert await embed_missing(db) == 0
            await db.commit()
        poison = await db.scalar(select(Chunk).where(Chunk.content == "POISON chunk"))
        assert poison.embed_attempts == MAX_EMBED_ATTEMPTS and "400" in poison.embed_error
        calls = emb.calls
        assert await embed_missing(db) == 0  # no longer selected
        assert emb.calls == calls
        assert (await reembed(db, dry_run=True))["failed"] == 1


async def test_outage_does_not_count_against_chunks(make_user) -> None:
    alice = await make_user("alice")
    set_embedder(NoVectors())
    async with get_sessionmaker()() as db:
        await upsert_document(
            db, owner_id=alice.id, source_type="upload", source_ref="o", title="O", chunks=[ChunkIn("text")]
        )
        await db.commit()
        for _ in range(MAX_EMBED_ATTEMPTS + 1):
            assert await embed_missing(db) == 0
            await db.commit()
        assert await db.scalar(select(Chunk.embed_attempts)) == 0
    set_embedder(HashEmbedder())
    async with get_sessionmaker()() as db:
        assert await embed_missing(db) == 1
