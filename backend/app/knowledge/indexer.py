"""Document indexing: chunks, embeddings, visibility.

Rule R1 (design 01 §H6): no network call happens inside an open DB transaction. Indexing is split into
prepare (pure) -> embed (network, no transaction) -> write (short transaction that re-checks under a row lock).
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.knowledge.acl import Visibility
from app.knowledge.chunking import ChunkIn
from app.llm.embeddings import current_embed_model, embed_model_id, get_embedder, is_permanent_failure
from app.logs import log
from app.models import Chunk, Document, Project

Vector = list[float] | None


async def commit_before_network(db: AsyncSession) -> None:
    """End the session's transaction before a network call (rule R1).

    Commits whatever the caller has done so far, so call it only at a consistent point. Objects stay usable
    (expire_on_commit=False).
    """
    if db.in_transaction():
        await db.commit()


def content_hash(title: str, chunks: list[ChunkIn]) -> str:
    h = hashlib.sha256(title.encode())
    for c in chunks:
        h.update(c.content.encode())
        h.update(json.dumps(c.meta, sort_keys=True).encode())
    return h.hexdigest()


def _embed_text(title: str, chunk: ChunkIn) -> str:
    context = " > ".join([title, *chunk.meta.get("headings", [])])
    return f"{context}\n\n{chunk.content}"


@dataclass
class PreparedDoc:
    owner_id: int
    source_type: str
    source_ref: str | None  # None for a wiki page that doesn't exist yet
    title: str
    chunks: list[ChunkIn]
    digest: str
    texts: list[str] = field(default_factory=list)
    chunk_hashes: list[str] = field(default_factory=list)


@dataclass
class Embedded:
    vectors: list[Vector]
    model: str
    unchanged: bool = False  # the stored document already has this exact content


def prepare_document(
    *, owner_id: int, source_type: str, source_ref: str | None, title: str, chunks: list[ChunkIn]
) -> PreparedDoc:
    texts = [_embed_text(title, c) for c in chunks]
    return PreparedDoc(
        owner_id=owner_id,
        source_type=source_type,
        source_ref=source_ref,
        title=title,
        chunks=chunks,
        digest=content_hash(title, chunks),
        texts=texts,
        chunk_hashes=[hashlib.sha256(t.encode()).hexdigest() for t in texts],
    )


async def embed_prepared(p: PreparedDoc) -> Embedded:
    """Reuse stored vectors for unchanged chunks (one short read in its own session), embed the rest.

    Only vectors from the current embedding model are reused (design 01 §H10). Embedding failures leave None
    for those chunks; embed_missing() fills them later.
    """
    try:
        model: str | None = await current_embed_model()
    except Exception as exc:  # gateway down: store without vectors, backfill later
        log.warning("index.embed_deferred", chunks=len(p.chunks), error=str(exc))
        model = None
    known: dict[str, list[float]] = {}
    if p.source_ref is not None:
        async with get_sessionmaker()() as db:
            doc = await db.scalar(
                select(Document).where(
                    Document.owner_id == p.owner_id,
                    Document.source_type == p.source_type,
                    Document.source_ref == p.source_ref,
                )
            )
            if doc is not None and doc.content_hash == p.digest:
                return Embedded(vectors=[], model=model or embed_model_id(), unchanged=True)
            if doc is not None and model is not None:
                rows = await db.execute(
                    select(Chunk.content_hash, Chunk.embedding).where(
                        Chunk.document_id == doc.id, Chunk.embed_model == model, Chunk.embedding.is_not(None)
                    )
                )
                known = {h: list(v) for h, v in rows if h}
    if model is None:
        return Embedded(vectors=[None] * len(p.chunks), model=embed_model_id())
    todo = [i for i, h in enumerate(p.chunk_hashes) if h not in known]
    fresh: list[Vector] = [None] * len(todo)
    if todo:
        try:
            fresh = list(await get_embedder().embed([p.texts[i] for i in todo]))
        except Exception as exc:  # embedding service down: store without vectors, backfill later
            log.warning("index.embed_deferred", chunks=len(todo), error=str(exc))
    by_index = dict(zip(todo, fresh, strict=True))
    vectors = [known.get(h) if i not in by_index else by_index[i] for i, h in enumerate(p.chunk_hashes)]
    if todo:
        log.info("index.embedded", chunks=len(todo), reused=len(p.chunks) - len(todo))
    return Embedded(vectors=vectors, model=model)


async def write_document(
    db: AsyncSession,
    p: PreparedDoc,
    emb: Embedded,
    *,
    visibility: Visibility | None = None,
    project_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Document:
    """Create or refresh the document and its chunks in the caller's transaction (no network). Caller commits.

    visibility=None keeps an existing document's visibility (new documents start private).
    """
    if p.source_ref is None:
        raise ValueError("source_ref must be set before writing")
    doc = await db.scalar(
        select(Document)
        .where(
            Document.owner_id == p.owner_id,
            Document.source_type == p.source_type,
            Document.source_ref == p.source_ref,
        )
        .with_for_update()
    )
    if doc is None:
        doc = Document(
            owner_id=p.owner_id,
            source_type=p.source_type,
            source_ref=p.source_ref,
            title=p.title,
            content_hash="",
            visibility=visibility or "private",
            project_id=project_id,
            meta=meta or {},
        )
        db.add(doc)
        await db.flush()
    elif doc.content_hash == p.digest and (visibility is None or visibility == doc.visibility):
        return doc

    if visibility is not None and visibility != doc.visibility:
        await set_visibility(db, doc, visibility, actor_id=p.owner_id)
    doc.title = p.title
    doc.project_id = project_id if project_id is not None else doc.project_id
    if meta is not None:
        doc.meta = meta
    if doc.content_hash != p.digest:
        if emb.unchanged or len(emb.vectors) != len(p.chunks):
            # Content changed after embed_prepared() looked (concurrent writer); store without vectors.
            emb = Embedded(vectors=[None] * len(p.chunks), model=emb.model)
        await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        db.add_all(
            Chunk(
                document_id=doc.id,
                owner_id=doc.owner_id,
                visibility=doc.visibility,
                ord=i,
                content=c.content,
                meta=c.meta,
                embedding=v,
                content_hash=h,
                embed_model=emb.model if v is not None else None,
            )
            for i, (c, v, h) in enumerate(zip(p.chunks, emb.vectors, p.chunk_hashes, strict=True))
        )
        doc.content_hash = p.digest
        log.info("index.document", document_id=doc.id, source_type=p.source_type, chunks=len(p.chunks))
    await db.flush()
    return doc


async def upsert_document(
    db: AsyncSession,
    *,
    owner_id: int,
    source_type: str,
    source_ref: str,
    title: str,
    chunks: list[ChunkIn],
    visibility: Visibility | None = None,
    project_id: int | None = None,
    meta: dict[str, Any] | None = None,
) -> Document:
    """prepare -> commit -> embed -> write. Commits the caller's session first (rule R1); the caller commits after."""
    p = prepare_document(owner_id=owner_id, source_type=source_type, source_ref=source_ref, title=title, chunks=chunks)
    await commit_before_network(db)
    emb = await embed_prepared(p)
    return await write_document(db, p, emb, visibility=visibility, project_id=project_id, meta=meta)


MAX_EMBED_ATTEMPTS = 5


async def embed_missing(db: AsyncSession, batch: int = 256) -> int:
    """Fill vectors for chunks stored without one. Read, commit, embed, then write in a new transaction.

    If the batch fails, each chunk is retried on its own once, so one bad chunk can't block the others. A chunk
    whose input is refused (4xx) gets `embed_attempts += 1` and `embed_error`; after MAX_EMBED_ATTEMPTS it is no
    longer selected (the System page lists it). Outages (timeouts, 5xx, 429) don't count against chunks.
    Returns the number of chunks embedded.
    """
    rows = (
        await db.execute(
            select(Chunk.id, Chunk.content, Document.title, Chunk.meta)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.embedding.is_(None), Chunk.embed_attempts < MAX_EMBED_ATTEMPTS)
            .order_by(Chunk.id)
            .limit(batch)
        )
    ).all()
    await commit_before_network(db)
    if not rows:
        return 0
    texts = [_embed_text(r.title, ChunkIn(r.content, r.meta or {})) for r in rows]
    vectors: list[Vector] = [None] * len(rows)
    errors: dict[int, str] = {}
    try:
        vectors = list(await get_embedder().embed(texts))
    except Exception as exc:
        if not is_permanent_failure(exc):
            log.warning("index.embed_missing_unavailable", chunks=len(rows), error=str(exc)[:300])
            return 0
        for i, t in enumerate(texts):
            try:
                [vectors[i]] = await get_embedder().embed([t])
            except Exception as one:
                if not is_permanent_failure(one):  # the service went away mid-way: stop, retry next tick
                    log.warning("index.embed_missing_unavailable", error=str(one)[:300])
                    break
                errors[i] = str(one)[:1000]
    model = embed_model_id()
    done = 0
    for i, r in enumerate(rows):
        if vectors[i] is not None:
            await db.execute(
                update(Chunk)
                .where(Chunk.id == r.id, Chunk.embedding.is_(None))
                .values(
                    embedding=vectors[i],
                    embed_model=model,
                    embed_error=None,
                    content_hash=hashlib.sha256(texts[i].encode()).hexdigest(),
                )
            )
            done += 1
        elif i in errors:
            await db.execute(
                update(Chunk)
                .where(Chunk.id == r.id)
                .values(embed_attempts=Chunk.embed_attempts + 1, embed_error=errors[i])
            )
    if errors:
        log.warning("index.embed_rejected", chunks=len(errors))
    log.info("index.embedded_missing", chunks=done)
    return done


async def reembed(db: AsyncSession, *, dry_run: bool) -> dict[str, Any]:
    """Clear vectors from other embedding models so embed_missing() refills them (design 01 §H10).

    Search ignores such vectors anyway (keyword search still finds those chunks). Clears in batches of
    5 000 rows, each in its own short transaction. The caller must have committed (the model probe is a
    network call).
    """
    await commit_before_network(db)
    model = await current_embed_model()
    by_model = dict(
        (
            await db.execute(
                select(Chunk.embed_model, func.count())
                .where(Chunk.embedding.is_not(None), Chunk.embed_model.is_distinct_from(model))
                .group_by(Chunk.embed_model)
            )
        ).all()
    )
    report: dict[str, Any] = {
        "current_model": model,
        "other_models": {str(k): v for k, v in by_model.items()},
        "missing": await db.scalar(
            select(func.count()).where(Chunk.embedding.is_(None), Chunk.embed_attempts < MAX_EMBED_ATTEMPTS)
        ),
        "failed": await db.scalar(select(func.count()).where(Chunk.embed_attempts >= MAX_EMBED_ATTEMPTS)),
        "cleared": 0,
    }
    await db.commit()
    if dry_run:
        return report
    while True:
        ids = (
            select(Chunk.id).where(Chunk.embedding.is_not(None), Chunk.embed_model.is_distinct_from(model)).limit(5000)
        )
        result = await db.execute(
            update(Chunk)
            .where(Chunk.id.in_(ids.scalar_subquery()))
            .values(embedding=None, embed_model=None, embed_attempts=0, embed_error=None)
        )
        await db.commit()
        report["cleared"] += result.rowcount
        if result.rowcount == 0:
            break
    log.info("index.reembed", **{k: v for k, v in report.items() if k != "other_models"})
    return report


async def set_visibility(db: AsyncSession, doc: Document, visibility: Visibility, *, actor_id: int) -> None:
    doc.visibility = visibility
    if visibility == "public":
        doc.published_by, doc.published_at = actor_id, datetime.now(UTC)
    else:
        doc.published_by, doc.published_at = None, None
    await db.execute(update(Chunk).where(Chunk.document_id == doc.id).values(visibility=visibility))


async def delete_document(db: AsyncSession, owner_id: int, source_type: str, source_ref: str) -> None:
    await db.execute(
        delete(Document).where(
            Document.owner_id == owner_id, Document.source_type == source_type, Document.source_ref == source_ref
        )
    )


async def set_project_visibility(db: AsyncSession, project: Project, visibility: Visibility, *, actor_id: int) -> None:
    """Code and repo docs follow their project. The project card is a wiki page and is published on its own."""
    project.visibility = visibility
    doc_ids = select(Document.id).where(
        Document.project_id == project.id, Document.source_type.in_(["code", "repo_doc"])
    )
    published = (
        {"published_by": actor_id, "published_at": datetime.now(UTC)}
        if visibility == "public"
        else {"published_by": None, "published_at": None}
    )
    await db.execute(
        update(Document)
        .where(Document.id.in_(doc_ids), Document.visibility != visibility)
        .values(visibility=visibility, **published)
    )
    await db.execute(
        update(Chunk)
        .where(Chunk.document_id.in_(doc_ids), Chunk.visibility != visibility)
        .values(visibility=visibility)
    )
