# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Temporal interpretation over hybrid retrieval candidates.

Reuse, not reinvention: ``remembering.temporal`` owns the event model, the
deterministic reducer, and the T3 bitemporal engine. This module is the
narrow product seam:

1. associate retrieved candidates with temporal events (via
   ``source_id`` / ``evidence_refs`` / chunk ids — never via vector
   rank, file mtime, or arrival order);
2. annotate each candidate with a small, engine-justified status;
3. resolve explicit temporal standpoints (current / valid_at /
   as_known / bitemporal);
4. admit candidates per route: recall preserves history, influence
   suppresses only what positive temporal evidence proves obsolete
   as current guidance.

Unmodelled prose is never classified as current or stale: it rides as
``not_modelled`` and is never suppressed for lack of temporal
semantics. Suppression happens after retrieval, in the open, with
reason codes — never by hiding candidates from the trace.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .routing import MemoryRoute
from .storage import ScoredChunk

# -- reason codes (stable, asserted in tests) ---------------------------

R_CURRENT = "temporal.current"
R_HISTORICAL = "temporal.historical_retained"
R_SUPERSEDED = "temporal.superseded_as_current"
R_CORRECTED = "temporal.corrected_as_current"
R_FUTURE = "temporal.future_not_effective"
R_RETRACTED = "temporal.retracted_as_current"
R_NOT_MODELLED = "temporal.not_modelled"
R_INCOMPLETE = "temporal.incomplete_history"
R_UNKNOWN_SUBJECT = "temporal.unknown_subject"

SUPPRESSING_STATUSES = frozenset(
    {"superseded", "corrected", "retracted", "planned"})

SUPPRESS_DECISION = "suppress_for_current_influence"


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# -- standpoint -----------------------------------------------------------

VALID_MODES = ("current", "valid_at", "as_known", "bitemporal")


@dataclass(frozen=True)
class TemporalStandpoint:
    mode: str = "current"
    valid_at: str | None = None
    known_at: str | None = None

    def __post_init__(self) -> None:
        from remembering.temporal.ordering import parse_ts

        if self.mode not in VALID_MODES:
            raise ValueError(
                f"TEMPORAL_BAD_STANDPOINT:unknown mode {self.mode!r}: "
                f"expected one of {VALID_MODES}")
        if self.mode in ("valid_at", "bitemporal") and (
                self.valid_at is None
                or parse_ts(self.valid_at) is None):
            raise ValueError(
                "TEMPORAL_BAD_STANDPOINT:mode "
                f"{self.mode!r} requires a valid ISO-8601 valid_at")
        if self.mode in ("as_known", "bitemporal") and (
                self.known_at is None
                or parse_ts(self.known_at) is None):
            raise ValueError(
                "TEMPORAL_BAD_STANDPOINT:mode "
                f"{self.mode!r} requires a valid ISO-8601 known_at")
        for label, value in (("valid_at", self.valid_at),
                             ("known_at", self.known_at)):
            if value is not None and parse_ts(value) is None:
                raise ValueError(
                    f"TEMPORAL_BAD_TIMESTAMP:{label}={value!r} is not "
                    "ISO-8601")

    def to_dict(self) -> dict:
        return {"mode": self.mode, "valid_at": self.valid_at,
                "known_at": self.known_at}


def standpoint_for_route(route: MemoryRoute,
                         requested: dict | None,
                         now: str) -> TemporalStandpoint:
    """Defaults: influence resolves current-as-of-now; recall carries
    no standpoint unless the caller supplies one explicitly (never
    guess valid_at from prose)."""
    requested = requested or {}
    mode = str(requested.get("mode", "") or "").strip().lower()
    if route is MemoryRoute.INFLUENCE and not mode:
        return TemporalStandpoint(mode="current", known_at=now)
    if not mode:
        # Recall without an explicit standpoint: interpret what the
        # temporal record says, suppress nothing.
        return TemporalStandpoint(mode="current", known_at=now)
    try:
        return TemporalStandpoint(
            mode=mode, valid_at=requested.get("valid_at"),
            known_at=requested.get("known_at"))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


# -- association + annotation ----------------------------------------------

@dataclass
class CandidateTemporal:
    chunk_id: str
    source_id: str
    status: str  # current|historical|superseded|corrected|planned|
    #           # retracted|not_modelled
    subject: str | None = None
    state_key: str | None = None
    event_ids: list[str] = field(default_factory=list)
    current_event_id: str | None = None
    reason: str = R_NOT_MODELLED
    incomplete_history: bool = False

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "temporal_status": self.status,
            "subject": self.subject,
            "state_key": self.state_key,
            "event_ids": list(self.event_ids),
            "current_event_id": self.current_event_id,
            "reason": self.reason,
            "incomplete_history": self.incomplete_history,
        }


def event_associates(event, candidate: ScoredChunk) -> bool:
    """Positive linkage only: the event names this candidate's source
    (as its own source or an evidence ref) or its exact chunk id.
    Retrieval rank, mtime, and arrival order confer nothing."""
    refs = set(event.evidence_refs or ())
    return (candidate.source_id == event.source_id
            or candidate.source_id in refs
            or candidate.chunk_id in refs)


def annotate_candidates(candidates: list[ScoredChunk], log,
                        now: str | None = None) -> list[CandidateTemporal]:
    """Attach engine-justified temporal standing to each candidate."""
    from remembering.temporal.ordering import parse_ts
    from remembering.temporal.query import TemporalEngine, log_has_gap
    from remembering.temporal.reducer import replay
    from remembering.temporal.ordering import temporal_sort

    now = now or utcnow_iso()
    engine = TemporalEngine(log)
    gap = log_has_gap(log)
    ordered = temporal_sort(log.events(), log.by_id())
    projection = replay(ordered)
    # The current event per subject: the latest state-moving event (in
    # temporal order) whose value equals the resolved current value.
    # Engine provenance (last transitions) is trace context, not
    # standing: history retains superseded events by design.
    moving = {"FACT_ESTABLISHED", "STATE_CHANGED", "REVISION_MADE",
              "CORRECTION_RECORDED", "TASK_CREATED", "TASK_COMPLETED"}
    current_event_of: dict[str, str | None] = {}
    for subject, state in projection.items():
        current_event_of[subject] = None
        if state.current is None:
            continue
        for event in ordered:
            if (event.subject == subject and event.event_type in moving
                    and event.value == state.current):
                current_event_of[subject] = event.event_id

    annotated: list[CandidateTemporal] = []
    for candidate in candidates:
        linked = [e for e in log.events()
                  if event_associates(e, candidate)]
        if not linked:
            annotated.append(CandidateTemporal(
                chunk_id=candidate.chunk_id, source_id=candidate.source_id,
                status="not_modelled", reason=R_NOT_MODELLED,
                incomplete_history=gap))
            continue
        subjects = sorted({e.subject for e in linked})
        subject = subjects[0]
        state = projection.get(subject)
        mine = {e.event_id for e in linked}
        current_event = current_event_of.get(subject)
        if current_event is not None and current_event in mine:
            status, reason = "current", R_CURRENT
        elif state is not None and any(
                eid in (state.superseded or ()) for eid in mine):
            status, reason = "superseded", R_SUPERSEDED
        elif state is not None and any(
                eid in (state.corrections or ()) for eid in mine):
            # Corrections list holds the correction event ids; a linked
            # *original* stays historical, a linked *correction* that is
            # not current is still the corrected understanding.
            status, reason = "corrected", R_CORRECTED
        elif any(e.event_type == "RETRACTION_RECORDED" for e in linked):
            status, reason = "retracted", R_RETRACTED
        elif any(e.event_type == "DECISION_MADE" and e.value
                 and e.effective_from
                 and parse_ts(e.effective_from) is not None
                 and parse_ts(now) is not None
                 and parse_ts(e.effective_from) > parse_ts(now)
                 for e in linked):
            status, reason = "planned", R_FUTURE
        else:
            status, reason = "historical", R_HISTORICAL
        annotated.append(CandidateTemporal(
            chunk_id=candidate.chunk_id, source_id=candidate.source_id,
            status=status, subject=subject,
            state_key=(state.state_key if state else None) or None,
            event_ids=sorted(mine), current_event_id=current_event,
            reason=reason, incomplete_history=gap))
    return annotated


# -- route-aware admission ---------------------------------------------------

@dataclass
class Suppression:
    chunk_id: str
    source_id: str
    retrieved: bool = True
    selected: bool = False
    temporal_status: str = ""
    decision: str = SUPPRESS_DECISION
    reason: str = R_SUPERSEDED
    superseded_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidate": self.chunk_id,
            "source_id": self.source_id,
            "retrieved": self.retrieved,
            "selected": self.selected,
            "temporal_status": self.temporal_status,
            "decision": self.decision,
            "reason": self.reason,
            "superseded_by": list(self.superseded_by),
        }


def admit_for_route(annotated: list[CandidateTemporal],
                    route: MemoryRoute) -> tuple[list, list]:
    """Recall selects everything (history preserved); influence
    suppresses only positive-evidence obsolescence. Returns
    (selected, suppressed)."""
    if route is MemoryRoute.RECALL:
        return list(annotated), []
    selected: list[CandidateTemporal] = []
    suppressed: list[Suppression] = []
    for item in annotated:
        if item.status in SUPPRESSING_STATUSES:
            reason = {
                "superseded": R_SUPERSEDED,
                "corrected": R_CORRECTED,
                "retracted": R_RETRACTED,
                "planned": R_FUTURE,
            }[item.status]
            by = ([item.current_event_id] if item.current_event_id
                  else [])
            suppressed.append(Suppression(
                chunk_id=item.chunk_id, source_id=item.source_id,
                temporal_status=item.status, reason=reason,
                superseded_by=by))
        else:
            selected.append(item)
    return selected, suppressed


# -- subject resolution ---------------------------------------------------------

def resolve_subject(log, subject: str, standpoint: TemporalStandpoint,
                    trajectory_limit: int = 50) -> dict:
    """Answer one subject under an explicit standpoint, with
    trajectory and gap visibility."""
    from remembering.temporal.query import TemporalEngine, log_has_gap

    engine = TemporalEngine(log)
    gap = log_has_gap(log)
    if standpoint.mode == "current":
        answer = engine.current(subject, known_at=standpoint.known_at)
    elif standpoint.mode == "valid_at":
        assert standpoint.valid_at is not None
        answer = engine.at(subject, standpoint.valid_at)
    elif standpoint.mode == "as_known":
        assert standpoint.known_at is not None
        answer = engine.as_known(subject, standpoint.known_at)
    else:
        assert standpoint.mode == "bitemporal"
        assert standpoint.valid_at is not None
        answer = engine.bitemporal(subject, standpoint.valid_at,
                                   standpoint.known_at)
    trajectory = engine.history(subject)
    truncated = len(trajectory) > trajectory_limit
    if truncated:
        trajectory = trajectory[-trajectory_limit:]
    return {
        "subject": subject,
        "value": answer.value,
        "status": answer.status,
        "detail": answer.detail,
        "provenance": list(answer.provenance),
        "standpoint": standpoint.to_dict(),
        "trajectory": trajectory,
        "trajectory_truncated": truncated,
        "incomplete_history": gap,
        "reason": (R_INCOMPLETE if gap and answer.value is not None
                   else (R_UNKNOWN_SUBJECT if answer.value is None
                         else R_CURRENT)),
    }


# -- Stage 3 contract evaluation -----------------------------------------------
# In-memory logs only (no services): each case names its category so
# the stage stays independently measurable. Counts never mix with the
# Stage 2 routing evaluation.

_SEQ = 0


def _E(event_id, subject, event_time, recorded_at, value="",
       event_type="STATE_CHANGED", state_key="database",
       effective_from=None, supersedes=(), corrects=(),
       source_id="acceptance", source_seq=None, evidence_refs=()):
    from remembering.temporal.model import EventEnvelope

    global _SEQ
    if source_seq is None:
        _SEQ += 1
        source_seq = _SEQ
    return EventEnvelope(
        event_id=event_id, source_id=source_id, source_seq=source_seq,
        event_type=event_type, subject=subject, state_key=state_key,
        value=value, event_time=event_time, recorded_at=recorded_at,
        effective_from=effective_from, supersedes=tuple(supersedes),
        corrects=tuple(corrects), evidence_refs=tuple(evidence_refs))


def _log_of(events):
    from remembering.temporal.log import EventLog

    log = EventLog()
    for event in events:
        log.append(event, event.recorded_at)
    return log


def _sqlite_pg_log():
    return _log_of([
        _E("cache-db-sqlite", "cache.metadata.database",
           "2026-07-01T10:00:00Z", "2026-07-01T10:05:00Z", value="SQLite",
           effective_from="2026-07-01T10:00:00Z"),
        _E("cache-db-postgres", "cache.metadata.database",
           "2026-08-20T10:00:00Z", "2026-08-20T10:05:00Z",
           value="PostgreSQL", effective_from="2026-08-20T10:00:00Z",
           supersedes=("cache-db-sqlite",)),
    ])


def _mkcandidate(chunk_id, source_id, text="evidence"):
    return ScoredChunk(chunk_id=chunk_id, source_id=source_id, text=text,
                       section=None, score=1.0, rank=1)


def evaluate_temporal() -> dict:
    """Deterministic Stage 3 contract. Categories stay separate."""
    from remembering.temporal.query import TemporalEngine, log_has_gap

    results: dict[str, dict] = {}

    def record(category: str, ok: bool, note: str = "") -> None:
        entry = results.setdefault(category, {"correct": 0, "total": 0,
                                              "failures": []})
        entry["total"] += 1
        if ok:
            entry["correct"] += 1
        else:
            entry["failures"].append(note)

    subject = "cache.metadata.database"
    log = _sqlite_pg_log()
    engine = TemporalEngine(log)

    # current-state
    ans = engine.current(subject)
    record("current", ans.value == "PostgreSQL" and ans.status == "OK",
           f"current={ans.value}/{ans.status}")

    # historical valid-time (July -> SQLite)
    ans = engine.at(subject, "2026-07-20T00:00:00Z")
    record("valid_time", ans.value == "SQLite", f"july={ans.value}")

    # later valid-time (September -> PostgreSQL)
    ans = engine.at(subject, "2026-09-01T00:00:00Z")
    record("valid_time", ans.value == "PostgreSQL",
           f"september={ans.value}")

    # late arrival: event 2026-07-15, learned 2026-08-15
    late = _log_of([
        _E("late-pg-active", subject, "2026-07-15T00:00:00Z",
           "2026-08-15T00:00:00Z", value="PostgreSQL",
           effective_from="2026-07-15T00:00:00Z"),
    ])
    late_engine = TemporalEngine(late)
    before = late_engine.bitemporal(subject, "2026-07-20T00:00:00Z",
                                    "2026-08-01T00:00:00Z")
    record("known_at", before.value is None,
           f"pre-learning leaked value={before.value}")
    after = late_engine.bitemporal(subject, "2026-07-20T00:00:00Z",
                                   "2026-08-20T00:00:00Z")
    record("bitemporal", after.value == "PostgreSQL",
           f"post-learning={after.value}")

    # planned vs effective: decision effective 2026-09-01, ask August
    planned_log = _log_of([
        _E("sqlite-was", subject, "2026-07-01T10:00:00Z",
           "2026-07-01T10:05:00Z", value="SQLite",
           effective_from="2026-07-01T10:00:00Z"),
        _E("decide-pg", subject, "2026-08-01T10:00:00Z",
           "2026-08-01T10:05:00Z", value="PostgreSQL",
           event_type="DECISION_MADE", effective_from="2026-09-01T00:00:00Z"),
    ])
    planned_engine = TemporalEngine(planned_log)
    ans = planned_engine.current(subject)
    from remembering.temporal.ordering import temporal_sort as _tsort
    from remembering.temporal.reducer import replay as _replay

    _proj = _replay(_tsort(planned_log.events(), planned_log.by_id()))
    _state = _proj[subject]
    record("planned_effective",
           ans.value == "SQLite" and _state.planned == "PostgreSQL"
           and _state.planned_effective_from == "2026-09-01T00:00:00Z",
           f"august current={ans.value} planned={_state.planned}")

    # supersession admission: recall keeps, influence suppresses
    cands = [_mkcandidate("c-old", "adr-old.md"),
             _mkcandidate("c-new", "adr-new.md")]
    sup_log = _log_of([
        _E("cache-db-sqlite", subject, "2026-07-01T10:00:00Z",
           "2026-07-01T10:05:00Z", value="SQLite",
           effective_from="2026-07-01T10:00:00Z",
           evidence_refs=("adr-old.md",)),
        _E("cache-db-postgres", subject, "2026-08-20T10:00:00Z",
           "2026-08-20T10:05:00Z", value="PostgreSQL",
           effective_from="2026-08-20T10:00:00Z",
           supersedes=("cache-db-sqlite",),
           evidence_refs=("adr-new.md",)),
    ])
    annotated = annotate_candidates(cands, sup_log, now="2026-09-01T00:00:00Z")
    by_id = {a.chunk_id: a for a in annotated}
    record("supersession",
           by_id["c-old"].status == "superseded"
           and by_id["c-new"].status == "current",
           f"statuses={[ (a.chunk_id, a.status) for a in annotated ]}")
    sel, _ = admit_for_route(annotated, MemoryRoute.RECALL)
    record("supersession", len(sel) == 2, "recall dropped history")
    sel, sup = admit_for_route(annotated, MemoryRoute.INFLUENCE)
    record("supersession",
           [s.chunk_id for s in sel] == ["c-new"]
           and len(sup) == 1 and sup[0].reason == R_SUPERSEDED,
           f"influence sel={[s.chunk_id for s in sel]}")

    # correction: original preserved, current uses revision
    corr_log = _log_of([
        _E("backend-v15", subject, "2026-07-01T10:00:00Z",
           "2026-07-01T10:05:00Z", value="PostgreSQL 15",
           effective_from="2026-07-01T10:00:00Z"),
        _E("backend-v16-fix", subject, "2026-08-01T10:00:00Z",
           "2026-08-01T10:05:00Z", value="PostgreSQL 16",
           event_type="CORRECTION_RECORDED",
           effective_from="2026-08-01T10:00:00Z",
           corrects=("backend-v15",)),
    ])
    corr_engine = TemporalEngine(corr_log)
    ans = corr_engine.current(subject)
    record("correction", ans.value == "PostgreSQL 16",
           f"current={ans.value}")
    record("correction",
           "backend-v15" in [t["event"] for t in
                             corr_engine.history(subject)],
           "original lost from history")

    # out-of-order arrival determinism
    from remembering.temporal.reducer import projection_digest, replay
    from remembering.temporal.ordering import temporal_sort

    reversed_log = _log_of(list(reversed(_sqlite_pg_log().events())))
    d1 = projection_digest(replay(temporal_sort(
        log.events(), log.by_id())))
    d2 = projection_digest(replay(temporal_sort(
        reversed_log.events(), reversed_log.by_id())))
    record("ordering", d1 == d2, f"digests differ {d1} vs {d2}")

    # incomplete history surfaces, never silent certainty
    gap_log = _log_of([
        _E("g1", subject, "2026-07-01T10:00:00Z", "2026-07-01T10:05:00Z",
           value="SQLite", source_seq=1),
        _E("g3", subject, "2026-08-01T10:00:00Z", "2026-08-01T10:05:00Z",
           value="PostgreSQL", source_seq=3),
    ])
    record("incomplete_history", log_has_gap(gap_log),
           "gap not detected")
    gap_annotated = annotate_candidates(
        [_mkcandidate("c-g", "g.md")], gap_log)
    record("incomplete_history",
           gap_annotated[0].incomplete_history is True,
           "gap flag missing on candidates")

    # unmodelled prose: visible, never suppressed, never ranked as current
    plain = annotate_candidates([_mkcandidate("c-p", "notes.md")], log)
    record("unmodelled",
           plain[0].status == "not_modelled"
           and plain[0].reason == R_NOT_MODELLED,
           f"status={plain[0].status}")
    sel, sup = admit_for_route(plain, MemoryRoute.INFLUENCE)
    record("unmodelled", len(sel) == 1 and not sup,
           "unmodelled evidence suppressed")

    total = sum(v["total"] for v in results.values())
    correct = sum(v["correct"] for v in results.values())
    return {"categories": results, "checks_total": total,
            "checks_passed": correct, "passed": total == correct}
