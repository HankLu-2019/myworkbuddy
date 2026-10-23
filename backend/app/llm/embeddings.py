import asyncio
import hashlib
import math
from typing import Protocol

import httpx2

from app.config import get_settings
from app.logs import log

BATCH_SIZE = 32
_transport: httpx2.AsyncBaseTransport | None = None


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    """Tests route embedding calls to the fake gateway (tests/fakellm)."""
    global _transport
    _transport = transport


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


# The gateway's `model` field (or its x-litellm-model-name header when the body only echoes the alias), e.g.
# amazon.titan-embed-text-v2:0. Shared by every GatewayEmbedder in the process
# (one per user key), so the model is resolved once and a switch is noticed whichever key sees it.
_resolved: str | None = None


class GatewayEmbedder:
    """Embeddings through the gateway. `api_key` is the user's virtual key (app.llm.client.embedder_for); the
    master key is used only for the model probe (`system_embedder`)."""

    def __init__(self, api_key: str | None = None) -> None:
        s = get_settings()
        self._url = f"{s.gateway_url}/v1/embeddings"
        key = api_key or s.gateway_key
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._model = s.embed_model
        self._dims = s.embed_dims

    @property
    def model_id(self) -> str:
        return _resolved or self._model

    async def resolve_model(self) -> str:
        """The concrete model behind the configured alias; one tiny embedding call the first time."""
        if _resolved is None:
            await self.embed(["model probe"])
        return self.model_id

    def _served_model(self, body_model: str | None, headers: httpx2.Headers) -> str | None:
        # LiteLLM sets the body's `model` to the requested alias; the deployment's model is only in this header.
        # A body naming another model (a gateway that doesn't rewrite it) is taken as is.
        if body_model and body_model != self._model:
            return body_model
        # Without the header the response can't tell a switch apart, so it keeps the model already resolved.
        return headers.get("x-litellm-model-name") or _resolved or body_model

    def _check_model(self, model: str | None) -> None:
        global _resolved
        model = model or self._model
        if _resolved is None or (_resolved == self._model and model != self._model):
            # The first answer may have carried only the alias; a later one naming the deployment refines it.
            _resolved = model
        elif model != _resolved:
            log.error("embed.model_changed", expected=_resolved, got=model)
            raise EmbedModelChanged(
                f"Embedding model changed from {_resolved} to {model}; restart the service and run mwb reembed"
            )

    async def _batch(self, client: httpx2.AsyncClient, texts: list[str]) -> list[list[float]]:
        for attempt in range(4):
            try:
                resp = await client.post(
                    self._url,
                    headers=self._headers,
                    json={"model": self._model, "input": texts, "dimensions": self._dims},
                )
                resp.raise_for_status()
                body = resp.json()
                self._check_model(self._served_model(body.get("model"), resp.headers))
                data = sorted(body["data"], key=lambda d: d["index"])
                if len(data) != len(texts):
                    raise ValueError(f"gateway returned {len(data)} embeddings for {len(texts)} inputs")
                return [_normalize(d["embedding"]) for d in data]
            except (httpx2.HTTPStatusError, httpx2.TransportError) as exc:
                retryable = isinstance(exc, httpx2.TransportError) or exc.response.status_code in (429, 500, 502, 503)
                if not retryable or attempt == 3:
                    raise
                log.warning("embed.retry", attempt=attempt, error=str(exc))
                await asyncio.sleep(2**attempt)
        raise AssertionError("unreachable")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx2.AsyncClient(timeout=120, transport=_transport) as client:
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
    if isinstance(exc, httpx2.HTTPStatusError):
        code = exc.response.status_code
        if "budget" in exc.response.text.lower():  # an exhausted budget (LiteLLM uses 400 or 429) is not the input
            return False
        return 400 <= code < 500 and code not in (401, 402, 403, 404, 408, 409, 429)
    return isinstance(exc, ValueError) and not isinstance(exc, EmbedModelChanged)


_embedder: Embedder | None = None  # test override
_system: GatewayEmbedder | None = None


def set_embedder(embedder: Embedder | None) -> None:
    """Tests use this to replace every embedder (user and system) with e.g. HashEmbedder."""
    global _embedder
    _embedder = embedder


def get_override() -> Embedder | None:
    return _embedder


def system_embedder() -> Embedder:
    """Master-key embedder, only for the model probe (`mwb reembed`) and maintenance. User work goes through
    app.llm.client.embedder_for."""
    global _system
    if _embedder is not None:
        return _embedder
    if _system is None:
        _system = GatewayEmbedder()
    return _system


def embed_model_id() -> str:
    """The model of the latest vectors in this process (the configured alias until the first call)."""
    if _embedder is not None:
        return getattr(_embedder, "model_id", None) or get_settings().embed_model
    return _resolved or get_settings().embed_model


async def current_embed_model() -> str:
    """Resolve the concrete model id, probing the gateway once if needed (a network call: rule R1)."""
    if _embedder is not None:
        return embed_model_id()
    return await system_embedder().resolve_model()  # type: ignore[union-attr]
