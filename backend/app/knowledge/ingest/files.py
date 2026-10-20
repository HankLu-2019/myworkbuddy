import hashlib
import io
from pathlib import PurePath

from fastapi import HTTPException, status
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.chunking import ChunkIn, chunk_markdown, chunk_plain
from app.knowledge.indexer import upsert_document
from app.models import Document

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log", ".yaml", ".yml", ".json", ".csv"}


def parse_upload(filename: str, data: bytes) -> list[ChunkIn]:
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf":
        chunks: list[ChunkIn] = []
        for page_no, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
            for c in chunk_plain(page.extract_text() or ""):
                c.meta["page"] = page_no
                chunks.append(c)
        return chunks
    if suffix not in TEXT_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type {suffix or '(none)'}; use PDF, Markdown or text",
        )
    text = data.decode("utf-8", errors="replace")
    return chunk_markdown(text) if suffix in {".md", ".markdown"} else chunk_plain(text)


async def ingest_upload(db: AsyncSession, owner_id: int, filename: str, data: bytes) -> Document:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File is larger than 20 MB")
    chunks = parse_upload(filename, data)
    if not chunks:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No text found in the file")
    name = PurePath(filename).name
    return await upsert_document(
        db,
        owner_id=owner_id,
        source_type="upload",
        source_ref=f"upload:{name}",
        title=name,
        chunks=chunks,
        meta={"filename": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
    )
