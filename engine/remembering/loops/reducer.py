# Bundled remembering engine (opencode-remembering product code).
"""Open-loop projector: canonical events in, derived loop states out.
Delete the projection and rebuild it from events for identical
semantic states. History lists actual events only — never generated
entries for derived status changes."""

from __future__ import annotations

from .events import transition_from_created
from .model import (
    L_CANCELLED,
    L_COMPLETED,
    L_OPEN_NO_EVIDENCE,
    L_SUPERSEDED,
    L_UNCERTAIN_CONFLICT,
    L_UNCERTAIN_INCOMPLETE,
    L_UNCERTAIN_MISSING,
    LoopState,
    LoopView,
    OpenLoopEvent,
)


def _parse_ts(value: str | None):
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def project(events: list[OpenLoopEvent],
            resolve_requirement,
            valid_at: str | None = None,
            known_at: str | None = None) -> dict[str, LoopView]:
    """Project loop states. resolve_requirement(requirement,
    valid_at, known_at) answers one closure requirement; requirements
    combine per the transition's closure_kind (all/any)."""
    ordered = sorted(events, key=lambda e: (
        e.event_time or "", e.recorded_at, e.event_id))
    cutoff = _parse_ts(known_at)
    if cutoff is not None:
        ordered = [e for e in ordered
                   if (_parse_ts(e.recorded_at) or cutoff) <= cutoff]
    valid = _parse_ts(valid_at)

    transitions: dict[str, dict] = {}
    histories: dict[str, list] = {}
    cancelled: set[str] = set()
    completed_claims: dict[str, list] = {}
    superseded_by: dict[str, str] = {}
    for event in ordered:
        if valid is not None:
            moment = _parse_ts(event.event_time)
            if moment is not None and moment > valid:
                continue
        histories.setdefault(event.loop_id, []).append({
            "event_id": event.event_id,
            "event_type": event.event_type,
            "event_time": event.event_time,
            "recorded_at": event.recorded_at,
            "evidence_refs": list(event.evidence_refs),
        })
        if event.event_type == "LOOP_CREATED":
            try:
                transition = transition_from_created(event)
            except (ValueError, KeyError, TypeError):
                continue
            transitions[event.loop_id] = {
                "transition": transition,
                "created_at": event.event_time,
            }
        elif event.event_type == "LOOP_CANCELLED":
            cancelled.add(event.loop_id)
        elif event.event_type == "LOOP_COMPLETED":
            completed_claims.setdefault(event.loop_id, []).append(event)
        elif event.event_type == "LOOP_SUPERSEDED":
            superseded_by[event.loop_id] = dict(
                event.payload).get("superseded_by", "") if isinstance(
                    dict(event.payload), dict) else ""

    views: dict[str, LoopView] = {}
    for loop_id, record in transitions.items():
        transition = record["transition"]
        history = histories.get(loop_id, [])
        evidence = sorted({ref for h in history for ref in
                           h["evidence_refs"]} | set(
                               transition.evidence_refs))
        views[loop_id] = _resolve_view(
            loop_id, transition, history, evidence, resolve_requirement,
            cancelled, completed_claims.get(loop_id, []),
            superseded_by.get(loop_id, ""), valid_at, known_at,
            record["created_at"])
    return views


def _resolve_view(loop_id, transition, history, evidence,
                  resolve_requirement, cancelled, claims, superseded_by,
                  valid_at, known_at, created_at) -> LoopView:
    base = {
        "loop_id": loop_id,
        "subject": transition.subject,
        "transition_kind": transition.transition_kind,
        "project_id": transition.project_id,
        "expected": {
            "from_state": transition.from_state,
            "expected_state": transition.expected_state,
            "closure_kind": transition.closure_kind,
            "closure": [r.to_dict() for r in transition.closure],
        },
        "evidence_refs": evidence,
        "history": history,
        "created_at": created_at,
    }
    if loop_id in cancelled:
        return LoopView(state=LoopState.CANCELLED, reason=L_CANCELLED,
                        resolved_at=_last_time(history), **base)
    if superseded_by:
        return LoopView(state=LoopState.SUPERSEDED, reason=L_SUPERSEDED,
                        resolved_at=_last_time(history), **base)
    if not transition.closure:
        return LoopView(state=LoopState.OPEN,
                        reason=L_OPEN_NO_EVIDENCE, **base)
    from .evidence import combine

    outcomes = [resolve_requirement(requirement, valid_at, known_at)
                for requirement in transition.closure]
    outcome = combine(outcomes, transition.closure_kind)
    # outcome: {satisfied, uncertain, conflict, missing, detail,
    #           search_complete}
    if outcome.get("conflict"):
        return LoopView(state=LoopState.UNCERTAIN,
                        reason=L_UNCERTAIN_CONFLICT,
                        closure=_closure_outcome(outcome), **base)
    if outcome.get("missing"):
        return LoopView(state=LoopState.UNCERTAIN,
                        reason=L_UNCERTAIN_MISSING,
                        closure=_closure_outcome(outcome), **base)
    if not outcome.get("search_complete", True):
        return LoopView(state=LoopState.UNCERTAIN,
                        reason=L_UNCERTAIN_INCOMPLETE,
                        closure=_closure_outcome(outcome), **base)
    if outcome.get("satisfied"):
        return LoopView(state=LoopState.COMPLETED, reason=L_COMPLETED,
                        resolved_at=_last_time(history),
                        closure=_closure_outcome(outcome), **base)
    if claims:
        from .model import L_OPEN_CLAIM_UNVERIFIED

        return LoopView(state=LoopState.OPEN,
                        reason=L_OPEN_CLAIM_UNVERIFIED,
                        closure=_closure_outcome(outcome), **base)
    return LoopView(state=LoopState.OPEN, reason=L_OPEN_NO_EVIDENCE,
                    closure=_closure_outcome(outcome), **base)


def _closure_outcome(outcome: dict) -> dict:
    return {"satisfied": bool(outcome.get("satisfied")),
            "uncertain": bool(outcome.get("uncertain", False)),
            "conflict": bool(outcome.get("conflict", False)),
            "missing": list(outcome.get("missing", [])),
            "detail": str(outcome.get("detail", "")),
            "search_complete": bool(outcome.get("search_complete", True))}


def _last_time(history: list) -> str:
    times = [h.get("event_time", "") for h in history if h.get("event_time")]
    return max(times) if times else ""
