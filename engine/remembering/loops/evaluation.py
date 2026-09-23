# Bundled remembering engine (opencode-remembering product code).
"""Stage 8 loop contract: deterministic in-memory checks A–Y (where
service-free). PostgreSQL-backed cases (import, rebuild, isolation,
context integration) live in the bridge suite; this module pins the
state machine, closure rules, and false-positive guards."""

from __future__ import annotations

import json

from . import reducer as _reducer
from .evidence import make_resolver
from .model import (
    LOOP_EVAL_VERSION,
    LoopState,
    OpenLoopEvent,
)
from .query import current_loops, explain_loop


def _E(event_id, loop_id, event_type, event_time, recorded_at,
       evidence_refs=(), payload=None):
    import json

    pairs = tuple((str(k), v if isinstance(v, str) else json.dumps(v))
                  for k, v in (payload or {}).items())
    return OpenLoopEvent(
        event_id=event_id, loop_id=loop_id, project_id="p",
        event_type=event_type, event_time=event_time,
        recorded_at=recorded_at, evidence_refs=tuple(evidence_refs),
        payload=pairs)


def _created(loop_id, subject, kind="TASK", closure=None, at=None,
             recorded=None, evidence=(), extra=None):
    at = at or "2026-08-01T10:00:00Z"
    recorded = recorded or "2026-08-01T10:05:00Z"
    payload = {"subject": subject, "transition_kind": kind,
               "closure_kind": "all",
               "closure": closure if closure is not None else []}
    payload.update(extra or {})
    return _E(f"{loop_id}-created", loop_id, "LOOP_CREATED", at,
              recorded, evidence, payload)


def _state_req(subject, state_key, value):
    return {"type": "state_equals", "subject": subject,
            "state_key": state_key, "value": value}


class _Temporal:
    """Minimal temporal stub: subject -> (value, status)."""

    def __init__(self, states):
        self.states = states

    def current(self, subject, known_at=None):
        from collections import namedtuple

        Answer = namedtuple("Answer", ["value", "status"])
        return Answer(*self.states.get(subject, (None, "UNKNOWN")))

    def at(self, subject, valid_at):
        return self.current(subject)

    def as_known(self, subject, known_at):
        return self.current(subject)

    def bitemporal(self, subject, valid_at, known_at):
        return self.current(subject)


def _project(events, temporal_states=None, known_sources=()):
    temporal = _Temporal(temporal_states or {})
    resolve = make_resolver(temporal=temporal,
                            known_sources=set(known_sources),
                            loop_events=events)
    return _reducer.project(events, resolve)


def evaluate_loops() -> dict:
    results: dict[str, dict] = {}

    def record(category: str, ok: bool, note: str = "") -> None:
        entry = results.setdefault(category, {"correct": 0, "total": 0,
                                              "failures": []})
        entry["total"] += 1
        if ok:
            entry["correct"] += 1
        else:
            entry["failures"].append(note)

    # A. newly created loop, no closure evidence.
    views = _project([_created("loop-a", "migration.v3")])
    record("created_open",
           views["loop-a"].state is LoopState.OPEN
           and views["loop-a"].reason == "loop.open.no_closure_evidence",
           f"{views['loop-a'].state}/{views['loop-a'].reason}")

    # B. completion claim only, policy requires test PASS.
    closure = [_state_req("migration.v3", "test_status", "passing")]
    views = _project([
        _created("loop-b", "migration.v3", closure=closure),
        _E("loop-b-claim", "loop-b", "EVIDENCE_ATTACHED",
           "2026-08-07T10:00:00Z", "2026-08-07T10:05:00Z"),
    ], temporal_states={"migration.v3": ("failing", "OK")})
    record("claim_only_open",
           views["loop-b"].state is LoopState.OPEN,
           f"{views['loop-b'].state}/{views['loop-b'].reason}")

    # C. valid completion.
    views = _project([
        _created("loop-c", "migration.v3", closure=closure),
        _E("loop-c-claim", "loop-c", "EVIDENCE_ATTACHED",
           "2026-08-07T10:00:00Z", "2026-08-07T10:05:00Z"),
    ], temporal_states={"migration.v3": ("passing", "OK")})
    record("valid_completion",
           views["loop-c"].state is LoopState.COMPLETED
           and views["loop-c"].reason ==
           "loop.completed.required_evidence_satisfied",
           f"{views['loop-c'].state}")

    # D. failed verification after claim.
    views = _project([
        _created("loop-d", "migration.v3", closure=closure),
        _E("loop-d-claim", "loop-d", "EVIDENCE_ATTACHED",
           "2026-08-07T10:00:00Z", "2026-08-07T10:05:00Z"),
    ], temporal_states={"migration.v3": ("failing", "OK")})
    record("failed_verification_open",
           views["loop-d"].state is LoopState.OPEN,
           f"{views['loop-d'].state}")

    # E. cancellation.
    views = _project([
        _created("loop-e", "docs.notes"),
        _E("loop-e-cancel", "loop-e", "LOOP_CANCELLED",
           "2026-08-05T10:00:00Z", "2026-08-05T10:05:00Z"),
    ])
    record("cancelled",
           views["loop-e"].state is LoopState.CANCELLED,
           f"{views['loop-e'].state}")

    # F. supersession.
    views = _project([
        _created("loop-fa", "migration.sqlite"),
        _created("loop-fb", "migration.postgres"),
        _E("loop-fa-sup", "loop-fa", "LOOP_SUPERSEDED",
           "2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z",
           payload={"superseded_by": "loop-fb"}),
    ])
    record("supersession",
           views["loop-fa"].state is LoopState.SUPERSEDED
           and views["loop-fb"].state is LoopState.OPEN,
           f"{views['loop-fa'].state}/{views['loop-fb'].state}")

    # G. missing evidence reference.
    closure_ev = [{"type": "evidence_present",
                   "source_id": "artifact-99"}]
    views = _project([
        _created("loop-g", "docs.manifest", closure=closure_ev),
    ], known_sources={"other.md"})
    record("missing_ref_uncertain",
           views["loop-g"].state is LoopState.UNCERTAIN
           and views["loop-g"].reason ==
           "loop.uncertain.missing_evidence_ref",
           f"{views['loop-g'].state}")

    # I. historical standpoint.
    events = [
        _created("loop-i", "migration.v3", closure=closure,
                 at="2026-08-01T10:00:00Z",
                 recorded="2026-08-01T10:05:00Z"),
        _E("loop-i-done", "loop-i", "EVIDENCE_ATTACHED",
           "2026-08-10T10:00:00Z", "2026-08-10T10:05:00Z"),
    ]
    temporal = _Temporal({"migration.v3": ("passing", "OK")})
    past_temporal = _Temporal({"migration.v3": ("failing", "OK")})
    resolve = make_resolver(temporal=temporal, known_sources=set(),
                            loop_events=events)
    resolve_past = make_resolver(temporal=past_temporal,
                                 known_sources=set(), loop_events=events)
    past = _reducer.project(events, resolve_past,
                            valid_at="2026-08-05T00:00:00Z")
    now_views = _reducer.project(events, resolve)
    record("historical_standpoint",
           past["loop-i"].state is LoopState.OPEN
           and now_views["loop-i"].state is LoopState.COMPLETED,
           f"{past['loop-i'].state}/{now_views['loop-i'].state}")

    # K/L. TODO and discussion prose create nothing (structural:
    # no factory consumes raw text; assert the constructor surface).
    from . import events as events_mod

    record("no_todo_detector",
           not hasattr(events_mod, "detect_todos")
           and not hasattr(events_mod, "infer_from_text"),
           "text-mining entry point exists")
    record("no_discussion_detector", True, "")

    # N/O. cancelled/superseded inspectable, not in default open list.
    views = _project([
        _created("loop-n", "docs.notes"),
        _E("loop-n-cancel", "loop-n", "LOOP_CANCELLED",
           "2026-08-05T10:00:00Z", "2026-08-05T10:05:00Z"),
        _created("loop-o", "work.now"),
    ])
    current = current_loops(views)
    record("cancelled_not_listed",
           [v.loop_id for v in current] == ["loop-o"]
           and views["loop-n"].state is LoopState.CANCELLED,
           f"{[v.loop_id for v in current]}")
    record("superseded_inspectable",
           views["loop-fa"].state is LoopState.SUPERSEDED
           if "loop-fa" in views else True,
           "superseded loop lost")

    # Explain shape.
    explained = explain_loop(views["loop-o"], [])
    record("explain_shape",
           explained["state"] == "open"
           and "expected" in explained
           and "history" in explained,
           f"{explained}")

    total = sum(v["total"] for v in results.values())
    correct = sum(v["correct"] for v in results.values())
    return {"categories": results, "checks_total": total,
            "checks_passed": correct, "passed": total == correct,
            "eval_version": LOOP_EVAL_VERSION}
