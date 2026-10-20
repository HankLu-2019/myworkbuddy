import asyncio
import hashlib
import math
from typing import Protocol

import httpx

from app.config import get_settings
from app.logs import log

BATCH_SIZE = 32


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class GatewayEmbedder:
    def __init__(self) -> None:
        s = get_settings()
        self._url = f"{s.gateway_url}/v1/embeddings"
        self._headers = {"Authorization": f"Bearer {s.gateway_key}"} if s.gateway_key else {}
        self._model = s.embed_model
        self._dims = s.embed_dims

    async def _batch(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        for attempt in range(4):
            try:
                resp = await client.post(
                    self._url,
                    headers=self._headers,
                    json={"model": self._model, "input": texts, "dimensions": self._dims},
                )
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                return [_normalize(d["embedding"]) for d in data]
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or exc.response.status_code in (429, 500, 502, 503)
                if not retryable or attempt == 3:
                    raise
                log.warning("embed.retry", attempt=attempt, error=str(exc))
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(0, len(texts), BATCH_SIZE):
                out.extend(await self._batch(client, texts[i : i + BATCH_SIZE]))
        return out


class HashEmbedder:
    """Deterministic bag-of-words embedder for tests: texts sharing words get similar vectors."""

    def __init__(self, dims: int = 1024) -> None:
        self.dims = dims

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in text.lower().split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
            out.append(_normalize(vec))
        return out


_embedder: Embedder | None = None


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = GatewayEmbedder()
    return _embedder
