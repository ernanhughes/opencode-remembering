# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""The baseline as one inspectable object: refresh, ask, verify."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import ingest
from .config import BaselineConfig
from .context import ContextConfig, ContextTrace, assemble
from .embeddings import EmbeddingProvider
from .generation import Answer, ExtractiveReader, OllamaGenerator
from .retrieval import Retriever, RetrievalTrace
from .storage import Store


@dataclass
class RefreshReport:
    discovered: int = 0
    indexed: int = 0
    unchanged: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)
    chunks: int = 0
    embedded: int = 0


@dataclass
class AskResult:
    query: str
    retrieval: RetrievalTrace | None
    context: ContextTrace
    answer: Answer


class Baseline:
    """Strong conventional RAG over project history in PostgreSQL."""

    def __init__(self, config: BaselineConfig, embedder: EmbeddingProvider):
        self.config = config
        self.embedder = embedder
        self.store = Store(config.dsn, config.schema)
        self.retriever = Retriever(self.store, embedder, config.retrieval)
        if config.generator.provider == "extractive":
            self.generator = ExtractiveReader()
        else:
            self.generator = OllamaGenerator(config.generator)

    @property
    def embedding_version(self) -> str:
        return self.embedder.version()

    def initialise(self) -> None:
        probe = self.embedder.embed(["dimension probe"])
        self.store.initialise(probe.dimension)

    def refresh(self, root: Path) -> RefreshReport:
        """discover → compare → add → update → remove stale → verify."""
        report = RefreshReport()
        paths = ingest.discover(root)
        report.discovered = len(paths)
        known = self.store.known_sources()
        seen: set[str] = set()
        version = self.embedder.version()
        for path in paths:
            source = ingest.parse(path, root)
            if source is None:
                report.failed.append(path.name)
                continue
            seen.add(source.source_id)
            if known.get(source.source_id) == source.content_hash:
                report.unchanged += 1
                continue
            is_new = source.source_id not in known
            self.store.upsert_source(
                source.source_id,
                source.artifact_type,
                source.content_hash,
                source.timestamp,
            )
            chunks = ingest.chunk_source(source, self.config.chunking)
            texts = [c.text for c in chunks]
            vectors = (
                self.embedder.embed(texts).vectors if texts else []
            )
            rows = [
                (
                    c.chunk_id, c.source_id, c.ordinal, c.text, c.section,
                    c.char_start, c.char_end, c.content_hash,
                    f"{self.config.chunking.policy}:"
                    f"{self.config.chunking.chunker_version}",
                    version, list(v),
                )
                for c, v in zip(chunks, vectors)
            ]
            self.store.replace_chunks(source.source_id, rows)
            report.chunks += len(rows)
            report.embedded += len(rows)
            report.indexed += 1
            if is_new:
                report.added += 1
            else:
                report.changed += 1
        for source_id in sorted(set(known) - seen):
            self.store.remove_source(source_id)
            report.removed += 1
        self.store.ensure_hnsw()
        self.verify()
        return report

    def verify(self) -> None:
        """Raise on any inconsistency the refresh should have fixed."""
        stats = self.store.stats()
        if self.store.orphan_chunks():
            raise RuntimeError("orphan chunks remain after refresh")
        versions = {
            item["version"] for item in stats["embedding_versions"]
        }
        if len(versions) > 1:
            raise RuntimeError(
                f"mixed embedding versions in store: {sorted(versions)}"
            )

    def ask(
        self,
        query: str,
        retrieval_override=None,
        context_override: ContextConfig | None = None,
    ) -> AskResult:
        """Ask with default, custom, or no retrieval.

        ``retrieval_override`` is None for the configured retriever,
        False for no retrieval (B0), or a RetrievalConfig for a variant.
        """
        if retrieval_override is False:
            empty = ContextTrace()
            return AskResult(
                query=query,
                retrieval=None,
                context=empty,
                answer=self.generator.answer(query, empty),
            )
        retriever = (
            self.retriever
            if retrieval_override is None
            else Retriever(self.store, self.embedder, retrieval_override)
        )
        retrieval = retriever.retrieve(query)
        context = assemble(
            retrieval.reranked, context_override or self.config.context
        )
        answer = self.generator.answer(query, context)
        return AskResult(
            query=query, retrieval=retrieval, context=context, answer=answer
        )

    def close(self) -> None:
        self.store.close()
