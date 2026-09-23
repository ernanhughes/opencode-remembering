# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Deterministic state reducer: event-sourcing transition semantics.

Generic rules operate on (subject, state_key, value, validity, relations)
— never on domain literals. A state is the current value per subject plus
its trajectory of transitions; decisions are recorded without changing
current state until their effective transition arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import REDUCER_VERSION, EventEnvelope


@dataclass
class Transition:
    before: str | None
    event_id: str
    event_type: str
    after: str | None
    effective_from: str | None = None


@dataclass
class SubjectState:
    subject: str
    state_key: str = ""
    current: str | None = None
    valid_from: str | None = None
    planned: str | None = None  # decided but not yet effective
    planned_effective_from: str | None = None
    history: list[Transition] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


def empty_projection() -> dict[str, SubjectState]:
    return {}


def _effective(event: EventEnvelope) -> str:
    return event.effective_from or event.event_time


def reduce_into(projection: dict[str, SubjectState],
                event: EventEnvelope) -> dict[str, SubjectState]:
    """Apply one event to the projection (mutates and returns it)."""
    state = projection.get(event.subject)
    if state is None:
        state = SubjectState(subject=event.subject,
                             state_key=event.state_key)
        projection[event.subject] = state
    before = state.current
    kind = event.event_type

    if kind == "FACT_ESTABLISHED":
        # An established fact moves state (Event Calculus initiates);
        # a bare observation enriches the record without moving it.
        if event.value:
            state.current = event.value
            state.valid_from = _effective(event)
        state.history.append(Transition(before, event.event_id, kind,
                                        state.current, _effective(event)))
    elif kind == "OBSERVATION_RECORDED":
        state.history.append(Transition(before, event.event_id, kind, before))
    elif kind == "DECISION_MADE":
        # A decision exists before it takes effect (decision_time !=
        # effective_time). Record intent; do not change current state.
        if event.value:
            state.planned = event.value
            state.planned_effective_from = event.effective_from
        state.history.append(Transition(before, event.event_id, kind, before,
                                        event.effective_from))
    elif kind in ("STATE_CHANGED", "REVISION_MADE"):
        if event.value:
            state.current = event.value
            state.valid_from = _effective(event)
            state.planned = None
            state.planned_effective_from = None
        for target in event.supersedes:
            if target not in state.superseded:
                state.superseded.append(target)
        state.history.append(Transition(before, event.event_id, kind,
                                        state.current, _effective(event)))
    elif kind == "CORRECTION_RECORDED":
        # Corrections append history; they never rewrite earlier lines.
        # If the correction carries a revised value for the same subject,
        # update current understanding while preserving the record.
        state.corrections.append(event.event_id)
        if event.value:
            state.history.append(Transition(before, event.event_id, kind,
                                            event.value, _effective(event)))
            state.current = event.value
            state.valid_from = _effective(event)
        else:
            state.history.append(Transition(before, event.event_id, kind,
                                            before))
    elif kind == "RETRACTION_RECORDED":
        state.history.append(Transition(before, event.event_id, kind, before))
    elif kind in ("TASK_CREATED", "TASK_COMPLETED"):
        if event.value:
            state.current = event.value
            state.valid_from = _effective(event)
        state.history.append(Transition(before, event.event_id, kind,
                                        state.current, _effective(event)))
    else:  # unknown kinds are recorded, never applied
        state.history.append(Transition(before, event.event_id, kind, before))
    return projection


def replay(events_in_temporal_order: list[EventEnvelope]) -> dict[str, SubjectState]:
    """Deterministic replay: same log + same reducer = same projection."""
    projection = empty_projection()
    for event in events_in_temporal_order:
        reduce_into(projection, event)
    return projection


def projection_digest(projection: dict[str, SubjectState]) -> str:
    import hashlib
    import json
    canonical = {subject: {
        "current": state.current,
        "valid_from": state.valid_from,
        "planned": state.planned,
        "history": [(t.before, t.event_id, t.event_type, t.after)
                    for t in state.history],
    } for subject, state in sorted(projection.items())}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


REDUCER_META = {"reducer_version": REDUCER_VERSION}
