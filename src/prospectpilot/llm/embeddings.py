"""Embedding providers. All return L2-normalized vectors of `settings.embedding_dim` (768)."""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol

import httpx

from prospectpilot.config import Settings, get_settings
from prospectpilot.obs.tracing import span

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _fit(v: list[float], dim: int) -> list[float]:
    return _normalize((v + [0.0] * dim)[:dim])


class HashEmbedder:
    """Signed feature hashing over unigrams + bigrams. Deterministic, offline, zero-cost.

    Not semantic, but lexical overlap is a fine retrieval signal for short insights, and it keeps
    CI and the demo free of model downloads.
    """

    name = "hash"

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        toks = _TOKEN.findall(text.lower())
        feats = toks + [f"{a}_{b}" for a, b in itertools.pairwise(toks)]
        v = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return _normalize(v)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_s)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = await self.client.post(
            f"{self.settings.ollama_base_url.rstrip('/')}/api/embed",
            json={"model": self.settings.ollama_embed_model, "input": texts},
        )
        r.raise_for_status()
        return [_fit(v, self.settings.embedding_dim) for v in r.json()["embeddings"]]


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
        self.client = httpx.AsyncClient(
            timeout=settings.llm_timeout_s, headers={"Authorization": f"Bearer {key}"}
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = await self.client.post(
            f"{self.settings.openai_base_url.rstrip('/')}/embeddings",
            json={
                "model": self.settings.openai_embed_model,
                "input": texts,
                "dimensions": self.settings.embedding_dim,
            },
        )
        r.raise_for_status()
        return [_fit(d["embedding"], self.settings.embedding_dim) for d in r.json()["data"]]


class TracedEmbedder:
    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self.name = inner.name

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with span("tool.embed", **{"pp.embedder": self.name, "pp.count": len(texts)}):
            return await self.inner.embed(texts)


_default: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    global _default
    if settings is None and _default is not None:
        return _default
    s = settings or get_settings()
    inner: Embedder
    if s.embedding_provider == "ollama":
        inner = OllamaEmbedder(s)
    elif s.embedding_provider == "openai":
        inner = OpenAIEmbedder(s)
    else:
        inner = HashEmbedder(s.embedding_dim)
    emb = TracedEmbedder(inner)
    if settings is None:
        _default = emb
    return emb
