import re
from dataclasses import dataclass, field
from typing import Any

MAX_CHARS = 3200  # ~800 tokens
OVERLAP = 200

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


@dataclass
class ChunkIn:
    content: str
    meta: dict[str, Any] = field(default_factory=dict)


def split_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        if len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            parts.append(current)
        while len(para) > max_chars:
            parts.append(para[:max_chars])
            para = para[max_chars - overlap :]
        current = para
    if current:
        parts.append(current)
    return parts


def chunk_markdown(text: str) -> list[ChunkIn]:
    """Split by headings, keeping the heading path as context on every chunk."""
    sections: list[tuple[list[str], str]] = []
    path: list[str] = []
    last = 0
    for m in _HEADING.finditer(text):
        sections.append((list(path), text[last : m.start()]))
        level = len(m.group(1))
        path = path[: level - 1] + [m.group(2).strip()]
        last = m.start()
    sections.append((list(path), text[last:]))

    chunks: list[ChunkIn] = []
    for heading_path, body in sections:
        for piece in split_text(body):
            chunks.append(ChunkIn(piece, {"headings": heading_path} if heading_path else {}))
    return chunks


def chunk_plain(text: str) -> list[ChunkIn]:
    return [ChunkIn(p) for p in split_text(text)]
