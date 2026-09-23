# Bundled remembering engine (opencode-remembering product code).
"""Open-loop domain model: expected transitions, append-only lifecycle
events, derived states. A loop is an expected transition whose closure
can be tested against evidence — never a TODO string, never a guess."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

OPEN_LOOP_SCHEMA = "open-loop-v0.1"
LOOP_EVENT_SCHEMA = "loop-event-v0.1"
LOOP_REDUCER_VERSION = "loop-reducer-v0.1"
LOOP_CLOSURE_VERSION = "loop-closure-v0.1"
LOOP_QUERY_VERSION = "loop-query-v0.1"
LOOP_CONTEXT_VERSION = "loop-context-v0.1"
LOOP_EVAL_VERSION = "loop-eval-v0.1"
LOOP_ENGINE_VERSION = "loops-engine-v0.1"

TRANSITION_KINDS = (
    "TASK",
    "STATE_CHANGE",
    "DELIVERABLE",
    "VALIDATION",
    "DECISION_FOLLOWUP",
    "REPAIR",
    "MIGRATION",
)

LOOP_EVENT_TYPES = frozenset({
    "LOOP_CREATED",
    "EVIDENCE_ATTACHED",
    "LOOP_COMPLETED",
    "LOOP_CANCELLED",
    "LOOP_SUPERSEDED",
})


class LoopState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    UNCERTAIN = "uncertain"


# Reason codes (stable, asserted in tests).
L_OPEN_NO_EVIDENCE = "loop.open.no_closure_evidence"
L_OPEN_CLAIM_UNVERIFIED = "loop.open.completion_claim_unverified"
L_COMPLETED = "loop.completed.required_evidence_satisfied"
L_CANCELLED = "loop.cancelled.explicit"
L_SUPERSEDED = "loop.superseded.explicit"
L_UNCERTAIN_INCOMPLETE = "loop.uncertain.closure_search_incomplete"
L_UNCERTAIN_MISSING = "loop.uncertain.missing_evidence_ref"
L_UNCERTAIN_CONFLICT = "loop.uncertain.conflicting_closure_evidence"


@dataclass(frozen=True)
class ClosureRequirement:
    """One testable closure condition. Small vocabulary only."""
    type: str  # state_equals | evidence_present | event_observed |
    #            # content_contains
    subject: str = ""
    state_key: str = ""
    value: str = ""
    source_id: str = ""
    event_type: str = ""
    text: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "subject": self.subject,
            "state_key": self.state_key,
            "value": self.value,
            "source_id": self.source_id,
            "event_type": self.event_type,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ClosureRequirement":
        kind = raw.get("type", "")
        if kind not in ("state_equals", "evidence_present",
                        "event_observed", "content_contains"):
            raise ValueError(
                f"LOOP_EVENT_INVALID:unknown closure type {kind!r}")
        return cls(
            type=kind,
            subject=str(raw.get("subject", "") or ""),
            state_key=str(raw.get("state_key", "") or ""),
            value=str(raw.get("value", "") or ""),
            source_id=str(raw.get("source_id", "") or ""),
            event_type=str(raw.get("event_type", "") or ""),
            text=str(raw.get("text", "") or ""),
        )


@dataclass(frozen=True)
class ExpectedTransition:
    loop_id: str
    project_id: str
    subject: str
    transition_kind: str
    from_state: str = ""
    expected_state: str = ""
    created_at: str = ""
    effective_from: str = ""
    evidence_refs: tuple[str, ...] = ()
    supersedes_loop_id: str = ""
    closure_kind: str = "all"
    closure: tuple[ClosureRequirement, ...] = ()
    schema_version: str = OPEN_LOOP_SCHEMA

    def to_dict(self) -> dict:
        return {
            "loop_id": self.loop_id,
            "project_id": self.project_id,
            "subject": self.subject,
            "transition_kind": self.transition_kind,
            "from_state": self.from_state,
            "expected_state": self.expected_state,
            "created_at": self.created_at,
            "effective_from": self.effective_from,
            "evidence_refs": list(self.evidence_refs),
            "supersedes_loop_id": self.supersedes_loop_id,
            "closure_kind": self.closure_kind,
            "closure": [r.to_dict() for r in self.closure],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class OpenLoopEvent:
    event_id: str
    loop_id: str
    project_id: str
    event_type: str
    event_time: str
    recorded_at: str
    evidence_refs: tuple[str, ...] = ()
    payload: tuple[tuple[str, str], ...] = ()
    schema_version: str = LOOP_EVENT_SCHEMA

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "loop_id": self.loop_id,
            "project_id": self.project_id,
            "event_type": self.event_type,
            "event_time": self.event_time,
            "recorded_at": self.recorded_at,
            "evidence_refs": list(self.evidence_refs),
            "payload": [list(p) for p in self.payload],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "OpenLoopEvent":
        payload_raw = raw.get("payload", ())
        if isinstance(payload_raw, dict):
            pairs = tuple(
                (str(k), v if isinstance(v, str) else json.dumps(v))
                for k, v in payload_raw.items())
        else:
            pairs = tuple((str(k), str(v)) for k, v in payload_raw)
        return cls(
            event_id=raw["event_id"],
            loop_id=raw["loop_id"],
            project_id=str(raw.get("project_id", "") or ""),
            event_type=raw["event_type"],
            event_time=raw["event_time"],
            recorded_at=raw["recorded_at"],
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            payload=pairs,
            schema_version=raw.get("schema_version", LOOP_EVENT_SCHEMA),
        )


@dataclass
class LoopView:
    """Derived state for one loop at a standpoint."""
    loop_id: str
    subject: str
    transition_kind: str
    state: LoopState = LoopState.OPEN
    reason: str = L_OPEN_NO_EVIDENCE
    project_id: str = ""
    expected: dict = field(default_factory=dict)
    evidence_refs: list = field(default_factory=list)
    closure: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    created_at: str = ""
    resolved_at: str = ""
    search_complete: bool = True

    def to_dict(self) -> dict:
        return {
            "loop_id": self.loop_id,
            "subject": self.subject,
            "transition_kind": self.transition_kind,
            "state": self.state.value,
            "reason": self.reason,
            "project_id": self.project_id,
            "expected": dict(self.expected),
            "evidence_refs": list(self.evidence_refs),
            "closure": dict(self.closure),
            "history": list(self.history),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "search_complete": self.search_complete,
        }
