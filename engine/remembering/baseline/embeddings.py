# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Pluggable embedding providers.

The default is a production-grade open embedding model served locally;
the choice is validated against the book's own retrieval tasks, never
assumed from a leaderboard. HashingEmbedder is a deterministic
test-only stand-in: it preserves none of the semantic properties the
baseline depends on and must never appear in a reported run.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimension: int


class EmbeddingProvider:
    name = "base"

    def embed(self, texts: list[str]) -> EmbeddingResult:
        raise NotImplementedError

    def version(self) -> str:
        raise NotImplementedError


def _post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class OllamaEmbeddingProvider(EmbeddingProvider):
    name = "ollama"

    def __init__(self, model: str, host: str = "http://localhost:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")
        self._dimension: int | None = None

    def embed(self, texts: list[str]) -> EmbeddingResult:
        out = _post_json(
            f"{self.host}/api/embed", {"model": self.model, "input": texts}
        )
        vectors = [list(map(float, v)) for v in out["embeddings"]]
        if self._dimension is None:
            self._dimension = len(vectors[0])
        return EmbeddingResult(
            vectors=vectors,
            provider=self.name,
            model=self.model,
            dimension=len(vectors[0]),
        )

    def version(self) -> str:
        return f"ollama:{self.model}"


class SentenceTransformerProvider(EmbeddingProvider):
    name = "sentence-transformers"

    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = model
        self._encoder = SentenceTransformer(model)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors = self._encoder.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return EmbeddingResult(
            vectors=[list(map(float, v)) for v in vectors],
            provider=self.name,
            model=self.model,
            dimension=len(vectors[0]),
        )

    def version(self) -> str:
        return f"sentence-transformers:{self.model}"


class HashingEmbedder(EmbeddingProvider):
    """Deterministic test double. NOT a retrieval baseline."""

    name = "hashing"

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dimension
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimension
                vec[index] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return EmbeddingResult(
            vectors=vectors,
            provider=self.name,
            model=f"hashing-{self.dimension}",
            dimension=self.dimension,
        )

    def version(self) -> str:
        return f"hashing:{self.dimension}"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
