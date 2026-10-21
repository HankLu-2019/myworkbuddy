import asyncio
import hashlib
import math
from typing import Protocol

import httpx

from app.config import get_settings
from app.logs import log

BATCH_SIZE = 32


class Embedder(Protocol):
    """`model_id` is the model that produced the last vectors (design 01 §H10); vectors are only comparable
    with vectors of the same model."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedModelChanged(RuntimeError):
    """The gateway answered with a different model than earlier in this process (e.g. a LiteLLM fallback or a
    config change behind the alias). Its vectors would be silently incomparable, so they are not used."""


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
        self._resolved: str | None = None  # the gateway's `model` field, e.g. amazon.titan-embed-text-v2:0

    @property
    def model_id(self) -> str:
        return self._resolved or self._model

    async def resolve_model(self) -> str:
        """The concrete model behind the configured alias; one tiny embedding call the first time."""
        if self._resolved is None:
            await self.embed(["model probe"])
        return self.model_id

    def _check_model(self, model: str | None) -> None:
        model = model or self._model
        if self._resolved is None:
            self._resolved = model
        elif model != self._resolved:
            log.error("embed.model_changed", expected=self._resolved, got=model)
            raise EmbedModelChanged(
                f"Embedding model changed from {self._resolved} to {model}; restart the service and run mwb reembed"
            )

    async def _batch(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        for attempt in range(4):
            try:
                resp = await client.post(
                    self._url,
                    headers=self._headers,
                    json={"model": self._model, "input": texts, "dimensions": self._dims},
                )
                resp.raise_for_status()
                body = resp.json()
                self._check_model(body.get("model"))
                data = sorted(body["data"], key=lambda d: d["index"])
                if len(data) != len(texts):
                    raise ValueError(f"gateway returned {len(data)} embeddings for {len(texts)} inputs")
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
        self.model_id = f"hash-{dims}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in text.lower().split():
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
            out.append(_normalize(vec))
        return out


def is_permanent_failure(exc: BaseException) -> bool:
    """True when the input itself was refused (it will fail again); False for outages worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in (401, 403, 404, 408, 409, 429)
    return isinstance(exc, ValueError) and not isinstance(exc, EmbedModelChanged)


_embedder: Embedder | None = None


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = GatewayEmbedder()
    return _embedder


def embed_model_id() -> str:
    """The model of the current embedder's latest vectors (configured alias until the first call)."""
    return getattr(get_embedder(), "model_id", None) or get_settings().embed_model


async def current_embed_model() -> str:
    """Resolve the concrete model id, probing the gateway once if needed (a network call: rule R1)."""
    resolve = getattr(get_embedder(), "resolve_model", None)
    return await resolve() if resolve else embed_model_id()
