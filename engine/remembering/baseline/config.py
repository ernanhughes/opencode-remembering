# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Baseline configuration. Every field lands in the run manifest."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ChunkingConfig:
    policy: str = "sentence"  # fixed | section | sentence
    target_chars: int = 2000
    overlap_chars: int = 300
    chunker_version: str = "0.1.0"


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "ollama"  # ollama | sentence-transformers | hashing
    model: str = "bge-m3"
    dimension: int = 1024
    ollama_host: str = "http://localhost:11434"

    @property
    def version(self) -> str:
        return f"{self.provider}:{self.model}:{self.dimension}"


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "hybrid"  # lexical | dense | hybrid
    candidate_k: int = 30
    lexical_k: int = 30
    dense_k: int = 30
    fusion_k: int = 60  # RRF constant
    reranker: str = "cross-encoder"  # none | cross-encoder | overlap
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rerank_k: int = 8


@dataclass(frozen=True)
class ContextConfig:
    max_chars: int = 6000
    max_passages: int = 6


@dataclass(frozen=True)
class GeneratorConfig:
    provider: str = "ollama"  # ollama | extractive
    model: str = "llama3.1:8b"
    ollama_host: str = "http://localhost:11434"
    temperature: float = 0.0
    system_prompt: str = (
        "Answer questions about a software project's recorded history. "
        "Use only the supplied project evidence. Quote or name the "
        "evidence identifiers you rely on. If the evidence does not "
        "determine an answer, say so plainly instead of guessing. "
        "Distinguish what was decided from what was merely proposed."
    )


@dataclass(frozen=True)
class BaselineConfig:
    dsn: str = "postgresql://postgres:postgres@localhost:5434/memory"
    schema: str = "baseline"
    chunking: ChunkingConfig = ChunkingConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    context: ContextConfig = ContextConfig()
    generator: GeneratorConfig = GeneratorConfig()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_env(cls) -> "BaselineConfig":
        return cls(
            dsn=os.environ.get(
                "MEMORY_BASELINE_DSN",
                "postgresql://postgres:postgres@localhost:5434/memory",
            )
        )
