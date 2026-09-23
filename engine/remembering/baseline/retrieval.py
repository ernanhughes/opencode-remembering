# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Retrieval: lexical, dense, hybrid fusion, reranking.

The pipeline keeps candidate retrieval and final ranking separate:
many candidates enter, few survive. Every stage records what it kept
and what it dropped, because the instrument must distinguish "never
retrieved" from "retrieved but not admitted".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import RetrievalConfig
from .embeddings import EmbeddingProvider
from .storage import ScoredChunk, Store


@dataclass
class RetrievalTrace:
    query: str
    lexical: list[ScoredChunk] = field(default_factory=list)
    dense: list[ScoredChunk] = field(default_factory=list)
    fused: list[ScoredChunk] = field(default_factory=list)
    reranked: list[ScoredChunk] = field(default_factory=list)
    latencies_ms: dict[str, float] = field(default_factory=dict)


def reciprocal_rank_fusion(
    rankings: list[list[ScoredChunk]], k: int = 60
) -> list[ScoredChunk]:
    """RRF: parameter-free fusion over ranks, robust to score scales."""
    scores: dict[str, float] = {}
    by_id: dict[str, ScoredChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (
                k + rank
            )
            by_id.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return [
        ScoredChunk(
            chunk_id=cid,
            source_id=by_id[cid].source_id,
            text=by_id[cid].text,
            section=by_id[cid].section,
            score=scores[cid],
            rank=i + 1,
        )
        for i, cid in enumerate(ordered)
    ]


class OverlapReranker:
    """Transparent test-double reranker: token overlap with the query."""

    name = "overlap"

    def rerank(
        self, query: str, chunks: list[ScoredChunk], k: int
    ) -> list[ScoredChunk]:
        query_tokens = set(query.lower().split())
        scored = []
        for chunk in chunks:
            chunk_tokens = set(chunk.text.lower().split())
            overlap = len(query_tokens & chunk_tokens)
            scored.append((overlap, chunk.chunk_id, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            ScoredChunk(c.chunk_id, c.source_id, c.text, c.section,
                        float(s), i + 1)
            for i, (s, _, c) in enumerate(scored[:k])
        ]


class CrossEncoderReranker:
    """Conventional cross-encoder reranking (joint query-passage scores)."""

    name = "cross-encoder"

    def __init__(self, model: str) -> None:
        from sentence_transformers import CrossEncoder

        self.model = model
        self._encoder = CrossEncoder(model)

    def rerank(
        self, query: str, chunks: list[ScoredChunk], k: int
    ) -> list[ScoredChunk]:
        pairs = [(query, chunk.text[:2000]) for chunk in chunks]
        scores = self._encoder.predict(pairs, show_progress_bar=False)
        ordered = sorted(
            zip(scores, chunks), key=lambda item: (-float(item[0]), item[1].chunk_id)
        )
        return [
            ScoredChunk(c.chunk_id, c.source_id, c.text, c.section,
                        float(s), i + 1)
            for i, (s, c) in enumerate(ordered[:k])
        ]


class Retriever:
    def __init__(
        self,
        store: Store,
        embedder: EmbeddingProvider,
        cfg: RetrievalConfig,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        self._reranker = None
        if cfg.reranker == "cross-encoder":
            self._reranker = CrossEncoderReranker(cfg.reranker_model)
        elif cfg.reranker == "overlap":
            self._reranker = OverlapReranker()

    def retrieve(self, query: str) -> RetrievalTrace:
        trace = RetrievalTrace(query=query)
        if self.cfg.mode in ("lexical", "hybrid"):
            started = time.perf_counter()
            trace.lexical = self.store.lexical_search(query, self.cfg.lexical_k)
            trace.latencies_ms["lexical"] = (
                time.perf_counter() - started
            ) * 1000.0
        if self.cfg.mode in ("dense", "hybrid"):
            started = time.perf_counter()
            vector = self.embedder.embed([query]).vectors[0]
            trace.dense = self.store.dense_search(vector, self.cfg.dense_k)
            trace.latencies_ms["dense"] = (
                time.perf_counter() - started
            ) * 1000.0
        if self.cfg.mode == "lexical":
            trace.fused = trace.lexical
        elif self.cfg.mode == "dense":
            trace.fused = trace.dense
        else:
            trace.fused = reciprocal_rank_fusion(
                [trace.lexical, trace.dense], k=self.cfg.fusion_k
            )
        if self._reranker is not None:
            started = time.perf_counter()
            trace.reranked = self._reranker.rerank(
                query, trace.fused, self.cfg.rerank_k
            )
            trace.latencies_ms["rerank"] = (
                time.perf_counter() - started
            ) * 1000.0
        else:
            trace.reranked = trace.fused[: self.cfg.rerank_k]
        return trace
