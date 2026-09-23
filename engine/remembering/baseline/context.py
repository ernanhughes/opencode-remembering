# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Context admission: from ranked candidates to a bounded context.

Retrieval proposes; admission disposes. Deduplication, per-source
diversity, and a hard character budget stand between the ranking and
the reader. Token counts are estimated as characters / 4 and labelled
as estimates wherever they are reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ContextConfig
from .storage import ScoredChunk

CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    return max(1, chars // CHARS_PER_TOKEN)


@dataclass
class ContextTrace:
    admitted: list[ScoredChunk] = field(default_factory=list)
    dropped_duplicates: int = 0
    dropped_over_budget: int = 0
    admitted_chars: int = 0
    admitted_tokens_estimate: int = 0
    sources_covered: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = []
        for chunk in self.admitted:
            parts.append(
                f"[source:{chunk.source_id} chunk:{chunk.chunk_id}]\n"
                f"{chunk.text}"
            )
        return "\n\n---\n\n".join(parts)


def assemble(
    ranked: list[ScoredChunk], cfg: ContextConfig
) -> ContextTrace:
    trace = ContextTrace()
    seen_hashes: set[str] = set()
    seen_sources: list[str] = []
    budget = cfg.max_chars
    for chunk in ranked:
        fingerprint = " ".join(chunk.text.lower().split())
        if fingerprint in seen_hashes:
            trace.dropped_duplicates += 1
            continue
        if len(trace.admitted) >= cfg.max_passages:
            trace.dropped_over_budget += 1
            continue
        if trace.admitted_chars + len(chunk.text) > budget and trace.admitted:
            trace.dropped_over_budget += 1
            continue
        seen_hashes.add(fingerprint)
        trace.admitted.append(chunk)
        trace.admitted_chars += len(chunk.text)
        if chunk.source_id not in seen_sources:
            seen_sources.append(chunk.source_id)
    trace.admitted_tokens_estimate = estimate_tokens(trace.admitted_chars)
    trace.sources_covered = seen_sources
    return trace
