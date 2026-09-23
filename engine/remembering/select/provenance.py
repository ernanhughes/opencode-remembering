# Bundled remembering engine (opencode-remembering product code).
"""Provenance closure: selected derived claims keep the minimum
grounding set required to stay inspectable. No provenance is
invented: a candidate without lineage metadata is grounded by its
own source/chunk identity."""

from __future__ import annotations

from .model import PROVENANCE_POLICY_VERSION, SelectionCandidate


def required_roots(candidate: SelectionCandidate,
                   roots_of) -> frozenset[str]:
    """Grounding roots for one candidate. Empty means self-grounded:
    the candidate's own source is its provenance boundary."""
    if not candidate.derived_from:
        return frozenset()
    return roots_of(candidate.source_id)


def closure(selected_ids: set[str],
            candidates: list[SelectionCandidate],
            roots_of) -> dict[str, list[str]]:
    """Map each selected derived chunk to grounding chunk ids present
    in the admitted pool. Roots are matched by source id; the best
    (lowest fused rank) chunk per root source is chosen."""
    by_source: dict[str, list[SelectionCandidate]] = {}
    for candidate in candidates:
        by_source.setdefault(candidate.source_id, []).append(candidate)
    for members in by_source.values():
        members.sort(key=lambda c: (c.fused_rank, c.chunk_id))
    out: dict[str, list[str]] = {}
    by_id = {c.chunk_id: c for c in candidates}
    for chunk_id in sorted(selected_ids):
        candidate = by_id.get(chunk_id)
        if candidate is None:
            continue
        grounding: list[str] = []
        for root in sorted(required_roots(candidate, roots_of)):
            pool = by_source.get(root, [])
            if pool and pool[0].chunk_id not in selected_ids:
                grounding.append(pool[0].chunk_id)
        if grounding:
            out[chunk_id] = grounding
    return out


def policy_version() -> str:
    return PROVENANCE_POLICY_VERSION
