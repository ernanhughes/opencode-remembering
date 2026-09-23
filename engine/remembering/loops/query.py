# Bundled remembering engine (opencode-remembering product code).
"""Loop queries: current states, historical standpoints, explanation.
Deterministic filters and ordering only — OPEN before UNCERTAIN,
then oldest unresolved first, then loop id. No priority scores."""

from __future__ import annotations

from .model import LoopState


def current_loops(views: dict, include_uncertain: bool = True,
                  limit: int = 20) -> list:
    states = {LoopState.OPEN}
    if include_uncertain:
        states.add(LoopState.UNCERTAIN)
    matched = [view for view in views.values() if view.state in states]
    return _order(matched)[:max(1, limit)]


def _order(views: list) -> list:
    rank = {LoopState.OPEN: 0, LoopState.UNCERTAIN: 1,
            LoopState.COMPLETED: 2, LoopState.CANCELLED: 3,
            LoopState.SUPERSEDED: 4}

    def key(view):
        return (rank.get(view.state, 9), view.created_at or "",
                view.loop_id)

    return sorted(views, key=key)


def filter_loops(views: dict, state: str | None = None,
                 subject: str | None = None,
                 transition_kind: str | None = None,
                 created_before: str | None = None,
                 created_after: str | None = None,
                 limit: int = 20) -> list:
    matched = list(views.values())
    if state is not None:
        try:
            wanted = LoopState(state.strip().lower())
        except ValueError:
            raise ValueError(
                f"LOOP_QUERY_INVALID:unknown state {state!r}")
        matched = [view for view in matched if view.state is wanted]
    if subject is not None:
        matched = [view for view in matched
                   if subject.lower() in view.subject.lower()]
    if transition_kind is not None:
        matched = [view for view in matched
                   if view.transition_kind == transition_kind.strip()]
    if created_before is not None:
        matched = [view for view in matched
                   if (view.created_at or "") < created_before]
    if created_after is not None:
        matched = [view for view in matched
                   if (view.created_at or "") >= created_after]
    return _order(matched)[:max(1, limit)]


def explain_loop(view, events: list) -> dict:
    """Why this state: creation, expectation, evidence seen,
    requirements with satisfaction, cancellation/supersession,
    completeness."""
    seen = [e for e in events if e.loop_id == view.loop_id]
    return {
        "loop_id": view.loop_id,
        "subject": view.subject,
        "transition_kind": view.transition_kind,
        "state": view.state.value,
        "state_reason": view.reason,
        "expected": view.expected,
        "closure": view.closure,
        "evidence_refs": view.evidence_refs,
        "history": view.history,
        "created_at": view.created_at,
        "resolved_at": view.resolved_at,
        "search_complete": view.search_complete,
        "event_count": len(seen),
    }
