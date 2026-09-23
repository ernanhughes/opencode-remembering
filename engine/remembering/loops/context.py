# Bundled remembering engine (opencode-remembering product code).
"""Derived loop candidates: relevant open-loop state as ordinary
pipeline input. Loops never bypass framing, trust, or selection:
they arrive as candidates with provenance and compete normally."""

from __future__ import annotations

from .model import LoopState


def relevant_loops(views: dict, objective: str = "",
                   work_type: str = "",
                   preferred_kinds: tuple[str, ...] = (),
                   evidence_sources: set[str] | None = None,
                   limit: int = 3) -> list:
    """Deterministic relevance: OPEN/UNCERTAIN loops scored by
    structural overlap (subject/objective tokens, kind preference,
    evidence already in the pool). No embeddings, no LLM."""
    evidence_sources = evidence_sources or set()
    objective_tokens = _tokens(objective)
    scored: list[tuple[int, str]] = []
    for view in views.values():
        if view.state not in (LoopState.OPEN, LoopState.UNCERTAIN):
            continue
        subject_tokens = _tokens(view.subject + " " +
                                 view.transition_kind)
        overlap = len(objective_tokens & subject_tokens)
        if overlap < 1:
            # Incidental evidence overlap alone never licenses a pull:
            # the loop must textually concern the current work.
            continue
        score = overlap * 2
        if preferred_kinds and view.transition_kind in preferred_kinds:
            score += 2
        if set(view.evidence_refs) & evidence_sources:
            score += 2
        scored.append((score, view.loop_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [loop_id for _, loop_id in scored[:max(1, limit)]]


def _tokens(text: str) -> set[str]:
    import re

    return {token for token in
            re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(token) > 3}


def loop_candidate(view, pool_sources: set[str] | None = None,
                   uncertainty_note: str = "") -> dict:
    """Render one derived candidate. Concise model-facing text with
    explicit state; UNCERTAIN never renders as unfinished. Evidence
    refs naming actual pool sources become structured lineage so
    Stage 5 revocation inheritance applies; other refs stay text-only
    (never unresolvable)."""
    pool_sources = pool_sources or set()
    """Render one derived candidate. Concise model-facing text with
    explicit state; UNCERTAIN never renders as unfinished."""
    if view.state is LoopState.UNCERTAIN:
        state_line = ("[open-loop state: uncertain] No verified closure "
                      "evidence was found in the recorded evidence "
                      "boundary.")
    else:
        state_line = (f"[open loop | {view.subject} | "
                      f"{view.state.value.upper()}]")
    expected = view.expected if isinstance(view.expected, dict) else {}
    lines = [
        state_line,
        f"Expected: {expected.get('expected_state', '') or view.transition_kind}",
        f"Current evidence: {view.closure.get('detail', '') if isinstance(view.closure, dict) else ''}",
        f"Basis: {', '.join(view.evidence_refs)}",
    ]
    if uncertainty_note:
        lines.append(uncertainty_note)
    derived = [ref for ref in view.evidence_refs if ref in pool_sources]
    return {
        "source_id": f"loop:{view.loop_id}",
        "chunk_id": f"loop:{view.loop_id}",
        "text": "\n".join(line for line in lines if line).strip()[:2000],
        "claim_key": f"loop:{view.loop_id}",
        "derived_from": derived,
        "role": "derived_restatement" if derived else "ordinary",
    }
