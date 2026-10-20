import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.acl import Visibility
from app.knowledge.chunking import ChunkIn
from app.llm.embeddings import get_embedder
from app.logs import log
from app.models import Chunk, Document, Project


def content_hash(title: str, chunks: list[ChunkIn]) -> str:
    h = hashlib.sha256(title.encode())
    for c in chunks:
        h.update(c.content.encode())
        h.update(json.dumps(c.meta, sort_keys=True).encode())
    return h.hexdigest()


def _embed_text(title: str, chunk: ChunkIn) -> str:
    context = " > ".join([title, *chunk.meta.get("headings", [])])
    return f"{context}\n\n{chunk.content}"


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
    """Create or refresh a document and its chunks. Re-embeds only when the content changed.

    visibility=None keeps an existing document's visibility (new documents start private).
    """
    digest = content_hash(title, chunks)
    doc = await db.scalar(
        select(Document).where(
            Document.owner_id == owner_id, Document.source_type == source_type, Document.source_ref == source_ref
        )
    )
    if doc is None:
        doc = Document(
            owner_id=owner_id,
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            content_hash="",
            visibility=visibility or "private",
            project_id=project_id,
            meta=meta or {},
        )
        db.add(doc)
        await db.flush()
    elif doc.content_hash == digest and (visibility is None or visibility == doc.visibility):
        return doc

    if visibility is not None and visibility != doc.visibility:
        await set_visibility(db, doc, visibility, actor_id=owner_id)
    doc.title = title
    doc.project_id = project_id if project_id is not None else doc.project_id
    if meta is not None:
        doc.meta = meta
    if doc.content_hash != digest:
        await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        vectors = await _embed_or_defer([_embed_text(title, c) for c in chunks])
        db.add_all(
            Chunk(
                document_id=doc.id,
                owner_id=doc.owner_id,
                visibility=doc.visibility,
                ord=i,
                content=c.content,
                meta=c.meta,
                embedding=v,
            )
            for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
        )
        doc.content_hash = digest
        log.info("index.document", document_id=doc.id, source_type=source_type, chunks=len(chunks))
    await db.flush()
    return doc


async def _embed_or_defer(texts: list[str]) -> list[list[float] | None]:
    """Embed now if possible; otherwise store chunks without vectors for embed_missing() to fill later."""
    if not texts:
        return []
    try:
        return await get_embedder().embed(texts)
    except Exception as exc:
        log.warning("index.embed_deferred", chunks=len(texts), error=str(exc))
        return [None] * len(texts)


async def embed_missing(db: AsyncSession, batch: int = 256) -> int:
    rows = (
        await db.execute(
            select(Chunk.id, Chunk.content, Document.title, Chunk.meta)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.embedding.is_(None))
            .limit(batch)
        )
    ).all()
    if not rows:
        return 0
    vectors = await get_embedder().embed([_embed_text(r.title, ChunkIn(r.content, r.meta or {})) for r in rows])
    for r, v in zip(rows, vectors, strict=True):
        await db.execute(update(Chunk).where(Chunk.id == r.id).values(embedding=v))
    log.info("index.embedded_missing", chunks=len(rows))
    return len(rows)


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
