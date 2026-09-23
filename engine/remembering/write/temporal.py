# Bundled remembering engine (opencode-remembering product code).
"""Temporal overlay for explicit-memory lineage (Stage 9 over Stage 3).

No second temporal system: each explicit-memory lineage (a record plus
its corrections/supersessions/retractions) owns one temporal subject,
``explicit-memory:<lineage root>``, and each accepted action appends
exactly one envelope to the existing temporal log:

- REMEMBER -> FACT_ESTABLISHED (moves current state).
- CORRECT -> CORRECTION_RECORDED with corrects=[prior event] (moves
  current state, preserves the original in history).
- SUPERSEDE -> STATE_CHANGED with supersedes=[prior event] (the prior
  value stays valid before the effective point).
- RETRACT -> RETRACTION_RECORDED against the target (suppresses
  current influence via annotation; history is preserved).

Linkage uses only explicit-memory source ids, so ordinary repository
sources never acquire an explicit-memory subject by accident.
"""

from __future__ import annotations

import hashlib


def lineage_subject(lineage_root: str) -> str:
    return f"explicit-memory:{lineage_root}"


def event_id_for(action_id: str, kind: str) -> str:
    digest = hashlib.sha256(
        f"{action_id}:{kind}".encode("utf-8")).hexdigest()[:32]
    return f"exm_{digest}"


def envelope_for_action(record, event, lineage_root: str,
                        prior_event_id: str | None,
                        source_seq: int):
    """Build the single temporal envelope for an accepted action."""
    from ..temporal.model import EventEnvelope

    from .model import ACTION_RELATION

    subject = lineage_subject(lineage_root)
    recorded = event.recorded_at
    effective = event.effective_from or recorded
    event_time = event.event_time or recorded
    action = event.action
    if action == "remember":
        kind = "FACT_ESTABLISHED"
        value = record.content if record is not None else ""
        source_id = record.source_id if record is not None else ""
        evidence = [source_id] if source_id else []
        supersedes: tuple[str, ...] = ()
        corrects: tuple[str, ...] = ()
    elif action == "correct":
        kind = "CORRECTION_RECORDED"
        value = record.content if record is not None else ""
        source_id = record.source_id if record is not None else ""
        evidence = [source_id] if source_id else []
        supersedes = ()
        corrects = (prior_event_id,) if prior_event_id else ()
    elif action == "supersede":
        kind = "STATE_CHANGED"
        value = record.content if record is not None else ""
        source_id = record.source_id if record is not None else ""
        evidence = [source_id] if source_id else []
        supersedes = (prior_event_id,) if prior_event_id else ()
        corrects = ()
    elif action == "retract":
        kind = "RETRACTION_RECORDED"
        value = ""
        target = event.target_record_id or ""
        source_id = f"memory://explicit/{target}" if target else ""
        evidence = [source_id] if source_id else []
        supersedes = ()
        corrects = ()
    else:  # pragma: no cover - gate rejects unknown actions first
        raise ValueError(f"WRITE_BAD_REQUEST:unknown action {action!r}")
    return EventEnvelope(
        event_id=event_id_for(event.action_id, kind),
        source_id=source_id,
        source_seq=source_seq,
        event_type=kind,
        subject=subject,
        state_key="memory",
        value=value,
        event_time=event_time,
        recorded_at=recorded,
        effective_from=effective,
        causal_parents=(),
        supersedes=supersedes,
        corrects=corrects,
        evidence_refs=tuple(evidence),
    )


def relation_kind(action: str) -> str | None:
    return ACTION_RELATION.get(action)
