from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.embeddings import get_embedder
from app.logs import log
from app.models import User

RRF_K = 60
CANDIDATES = 50


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    title: str
    source_type: str
    source_ref: str
    owner_id: int
    visibility: str
    project_id: int | None
    content: str
    meta: dict[str, Any]
    score: float


def _filters(source_types: list[str] | None, project_id: int | None) -> str:
    clauses = []
    if source_types:
        clauses.append("AND d.source_type = ANY(:types)")
    if project_id is not None:
        clauses.append("AND d.project_id = :pid")
    return " ".join(clauses)


async def hybrid_search(
    db: AsyncSession,
    user: User,
    query: str,
    *,
    source_types: list[str] | None = None,
    project_id: int | None = None,
    limit: int = 12,
) -> list[Hit]:
    """Vector + BM25 candidates, each restricted to what the user may see, fused by reciprocal rank."""
    query = query.strip()
    if not query:
        return []
    try:
        [qvec] = await get_embedder().embed([query])
    except Exception as exc:  # embedding service down: keyword search still answers
        log.warning("search.embed_failed", error=str(exc))
        qvec = None
    extra = _filters(source_types, project_id)
    acl = "(c.visibility = 'public' OR c.owner_id = :uid)"
    sem = (
        f"""
        SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS r
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE {acl} AND c.embedding IS NOT NULL {extra}
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :k"""
        if qvec
        else "SELECT NULL::bigint AS id, NULL::bigint AS r WHERE false"
    )
    sql = f"""
    WITH sem AS ({sem}
    ), lex AS (
        SELECT c.id, row_number() OVER (ORDER BY paradedb.score(c.id) DESC) AS r
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.content ||| :qtext AND {acl} {extra}
        ORDER BY paradedb.score(c.id) DESC
        LIMIT :k
    ), fused AS (
        SELECT id, sum(1.0 / ({RRF_K} + r)) AS score
        FROM (SELECT * FROM sem UNION ALL SELECT * FROM lex) t
        GROUP BY id
    )
    SELECT c.id, c.document_id, c.content, c.metadata, d.title, d.source_type, d.source_ref,
           d.owner_id, d.visibility, d.project_id, f.score
    FROM fused f
    JOIN chunks c ON c.id = f.id
    JOIN documents d ON d.id = c.document_id
    ORDER BY f.score DESC
    LIMIT :limit
    """
    params: dict[str, Any] = {
        "qtext": query,
        "uid": user.id,
        "k": CANDIDATES,
        "limit": limit,
    }
    if qvec:
        params["qvec"] = "[" + ",".join(f"{v:.6f}" for v in qvec) + "]"
    if source_types:
        params["types"] = source_types
    if project_id is not None:
        params["pid"] = project_id
    # Without iterative scans, HNSW returns the global top-k first and the ACL filter can then leave too few rows.
    await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        Hit(
            chunk_id=r["id"],
            document_id=r["document_id"],
            title=r["title"],
            source_type=r["source_type"],
            source_ref=r["source_ref"],
            owner_id=r["owner_id"],
            visibility=r["visibility"],
            project_id=r["project_id"],
            content=r["content"],
            meta=r["metadata"] or {},
            score=float(r["score"]),
        )
        for r in rows
    ]
