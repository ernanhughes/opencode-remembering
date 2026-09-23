# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Versioned event envelope and validation.

Every retained field has a semantic purpose:

- identity: event_id (global), source_id + source_seq (per-publisher order)
- kind: event_type (generic vocabulary), subject + state_key/value (fluent)
- time axes: event_time (occurred), recorded_at (learned/transaction),
  effective_from/effective_to (valid time of the state change)
- relations: causal_parents (explicit happens-before), supersedes,
  corrects (append-only correction pointer), evidence_refs (Ch7 support)
- payload: operator-carried detail; schema_version: envelope version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = "temporal-event-v0.1"
REDUCER_VERSION = "temporal-reducer-v0.1"

# Generic vocabulary. No domain literals live here.
EVENT_TYPES = frozenset({
    "FACT_ESTABLISHED",
    "OBSERVATION_RECORDED",
    "DECISION_MADE",
    "STATE_CHANGED",
    "REVISION_MADE",
    "CORRECTION_RECORDED",
    "RETRACTION_RECORDED",
    "TASK_CREATED",
    "TASK_COMPLETED",
})


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    source_id: str
    source_seq: int
    event_type: str
    subject: str
    event_time: str  # ISO-8601, when the represented event occurred
    recorded_at: str  # ISO-8601, when the memory system recorded it
    state_key: str = ""
    value: str = ""
    effective_from: str | None = None  # valid-time start (defaults: event)
    effective_to: str | None = None  # valid-time end (None = open)
    causal_parents: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    corrects: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    payload: tuple[tuple[str, str], ...] = ()
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "source_seq": self.source_seq,
            "event_type": self.event_type,
            "subject": self.subject,
            "state_key": self.state_key,
            "value": self.value,
            "event_time": self.event_time,
            "recorded_at": self.recorded_at,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "causal_parents": list(self.causal_parents),
            "supersedes": list(self.supersedes),
            "corrects": list(self.corrects),
            "evidence_refs": list(self.evidence_refs),
            "payload": [list(p) for p in self.payload],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "EventEnvelope":
        return cls(
            event_id=raw["event_id"],
            source_id=raw["source_id"],
            source_seq=int(raw["source_seq"]),
            event_type=raw["event_type"],
            subject=raw["subject"],
            event_time=raw["event_time"],
            recorded_at=raw["recorded_at"],
            state_key=raw.get("state_key", ""),
            value=raw.get("value", ""),
            effective_from=raw.get("effective_from"),
            effective_to=raw.get("effective_to"),
            causal_parents=tuple(raw.get("causal_parents", ())),
            supersedes=tuple(raw.get("supersedes", ())),
            corrects=tuple(raw.get("corrects", ())),
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            payload=tuple(tuple(p) for p in raw.get("payload", ())),
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
        )


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_event(event: EventEnvelope,
                   known_ids: set[str],
                   seen_source_seq: dict[str, set[int]],
                   strict_refs: bool = False) -> ValidationReport:
    """Structural + temporal validation for one envelope.

    Referential checks (causal parent / supersedes / correction targets)
    are deferred by default: out-of-order arrival must remain ingestible;
    missing references surface in health/unknown_refs instead of
    rejecting the write. Pass strict_refs=True for a closed-world audit.
    """
    errors: list[str] = []
    if not event.event_id:
        errors.append("EMPTY_EVENT_ID")
    if event.event_id in known_ids:
        errors.append(f"DUPLICATE_EVENT_ID:{event.event_id}")
    if event.event_type not in EVENT_TYPES:
        errors.append(f"UNKNOWN_EVENT_TYPE:{event.event_type}")
    if event.source_seq < 1:
        errors.append(f"BAD_SOURCE_SEQ:{event.source_seq}")
    if event.source_seq in seen_source_seq.get(event.source_id, set()):
        errors.append(f"DUPLICATE_SOURCE_SEQ:{event.source_id}:{event.source_seq}")
    if _parse_ts(event.event_time) is None:
        errors.append(f"BAD_EVENT_TIME:{event.event_time}")
    if _parse_ts(event.recorded_at) is None:
        errors.append(f"BAD_RECORDED_AT:{event.recorded_at}")
    eff_from = _parse_ts(event.effective_from) if event.effective_from else None
    eff_to = _parse_ts(event.effective_to) if event.effective_to else None
    if event.effective_from and eff_from is None:
        errors.append(f"BAD_EFFECTIVE_FROM:{event.effective_from}")
    if event.effective_to and eff_to is None:
        errors.append(f"BAD_EFFECTIVE_TO:{event.effective_to}")
    if eff_from and eff_to and eff_to < eff_from:
        errors.append("INVALID_INTERVAL:effective_to<effective_from")
    if event.schema_version != SCHEMA_VERSION:
        errors.append(f"UNSUPPORTED_SCHEMA:{event.schema_version}")
    if strict_refs:
        errors.extend(reference_errors(event, known_ids))
    return ValidationReport(ok=not errors, errors=errors)


def reference_errors(event: EventEnvelope,
                     known_ids: set[str]) -> list[str]:
    """Deferred referential checks (parents, supersedes, corrects)."""
    errors: list[str] = []
    for parent in event.causal_parents:
        if parent not in known_ids:
            errors.append(f"UNKNOWN_CAUSAL_PARENT:{parent}")
    for target in event.supersedes:
        if target not in known_ids:
            errors.append(f"UNKNOWN_SUPERSEDES_TARGET:{target}")
    for target in event.corrects:
        if target not in known_ids:
            errors.append(f"UNKNOWN_CORRECTION_TARGET:{target}")
    return errors


def check_causal_temporal(event: EventEnvelope,
                          by_id: dict[str, EventEnvelope]) -> list[str]:
    """Flag causal parents dated after the event (temporal-causality check).

    A MOTIVATED_BY-style parent must precede the event in event time.
    Later evidence can corroborate but cannot have motivated a past action.
    """
    violations: list[str] = []
    mine = _parse_ts(event.event_time)
    if mine is None:
        return violations
    for parent in event.causal_parents:
        other = by_id.get(parent)
        if other is None:
            continue
        theirs = _parse_ts(other.event_time)
        if theirs is not None and theirs > mine:
            violations.append(
                f"TEMPORAL_CAUSALITY_VIOLATION:{event.event_id}:"
                f"parent {parent} is later in event time")
    return violations
