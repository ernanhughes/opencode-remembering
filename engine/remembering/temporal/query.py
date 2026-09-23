# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Query layer: four resolver conditions + computed/materialized belief.

Conditions:
- T0 bag: ignores all temporal fields (event presence alone).
- T1 arrival: newest by ingest/record order wins (naive baseline).
- T1b event-time: sorted by event_time, last wins (no transitions).
- T2 ordered: event-time order + reducer transition semantics.
- T3 temporal: T2 + valid/record standpoint + effective-time +
  supersession/correction semantics (bitemporal).

The deterministic temporal engine answers mechanical questions with no
LLM. An LLM may later render these answers; it must not reinterpret them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .log import EventLog
from .model import EventEnvelope
from .ordering import parse_ts, temporal_sort
from .reducer import SubjectState, empty_projection, reduce_into, replay


@dataclass
class Answer:
    value: str | None
    status: str  # OK | UNCERTAIN | UNKNOWN | INCOMPLETE_HISTORY | STALE?
    detail: str = ""
    provenance: tuple[str, ...] = ()


class ResolverCondition:
    BAG = "T0-bag"
    ARRIVAL = "T1-arrival"
    EVENT_TIME = "T1b-event-time"
    ORDERED = "T2-ordered"
    TEMPORAL = "T3-temporal"


def _state_events(log: EventLog, subject: str) -> list[EventEnvelope]:
    return [e for e in log.events() if e.subject == subject]


def _known_filter(events: list[EventEnvelope], known_at: str | None) -> list[EventEnvelope]:
    if known_at is None:
        return events
    cutoff = parse_ts(known_at)
    return [e for e in events
            if parse_ts(e.recorded_at) is not None
            and parse_ts(e.recorded_at) <= cutoff]


class TemporalEngine:
    """Computed (replay at query) resolver over a frozen log."""

    def __init__(self, log: EventLog, condition: str = ResolverCondition.TEMPORAL):
        self.log = log
        self.condition = condition

    # -- current -----------------------------------------------------
    def current(self, subject: str,
                known_at: str | None = None) -> Answer:
        if self.condition == ResolverCondition.BAG:
            return self._bag_current(subject)
        if self.condition == ResolverCondition.ARRIVAL:
            return self._last_wins(subject, key="arrival", known_at=known_at)
        if self.condition == ResolverCondition.EVENT_TIME:
            return self._last_wins(subject, key="event", known_at=known_at)
        return self._reducer_current(subject, known_at=known_at)

    # -- bitemporal ---------------------------------------------------
    def at(self, subject: str, valid_at: str) -> Answer:
        """What was valid at valid_at (all knowledge)?"""
        return self.bitemporal(subject, valid_at=valid_at, known_at=None)

    def as_known(self, subject: str, known_at: str) -> Answer:
        """What did we believe at known_at about the then-current state?"""
        events = _known_filter(_state_events(self.log, subject), known_at)
        if not events:
            return Answer(None, "UNKNOWN", "no events known at cutoff", ())
        if self.condition in (ResolverCondition.BAG,):
            return self._bag_current(subject, events=events)
        if self.condition in (ResolverCondition.ARRIVAL, ResolverCondition.EVENT_TIME):
            key = "arrival" if self.condition == ResolverCondition.ARRIVAL else "event"
            return self._last_wins(subject, key=key, events=events)
        projection = replay(temporal_sort(events, self.log.by_id()))
        state = projection.get(subject)
        if state is None or state.current is None:
            return Answer(None, "UNKNOWN", "no state established", ())
        return Answer(state.current, "OK",
                      f"valid_from={state.valid_from}", self._prov(state))

    def bitemporal(self, subject: str, valid_at: str,
                   known_at: str | None) -> Answer:
        """Given what was known by known_at, what was valid at valid_at?"""
        events = _known_filter(_state_events(self.log, subject), known_at)
        if self.condition in (ResolverCondition.BAG, ResolverCondition.ARRIVAL,
                              ResolverCondition.EVENT_TIME):
            # Non-temporal conditions cannot take a valid_at standpoint.
            return self._last_wins(
                subject,
                key="arrival" if self.condition != ResolverCondition.EVENT_TIME else "event",
                events=events)
        if log_has_gap(self.log) and self.condition == ResolverCondition.TEMPORAL:
            pass  # gap noted in detail, not hidden
        ordered = temporal_sort(events, self.log.by_id())
        projection = empty_projection()
        best: Answer = Answer(None, "UNKNOWN", "no state covered valid_at", ())
        valid = parse_ts(valid_at)
        for event in ordered:
            reduce_into(projection, event)
            if event.event_type in ("OBSERVATION_RECORDED",
                                    "RETRACTION_RECORDED"):
                continue  # record-only: must not re-stamp the standpoint
            state = projection[subject]
            eff = parse_ts(event.effective_from or event.event_time)
            if valid is not None and eff is not None and eff <= valid \
                    and state.current is not None:
                best = Answer(state.current, "OK",
                              f"as of {event.event_id} eff={eff.isoformat()}",
                              self._prov(state))
        if log_has_gap(self.log):
            best = Answer(best.value,
                          best.status if best.value is None else "OK",
                          best.detail + " [INCOMPLETE_HISTORY: sequence gap]",
                          best.provenance)
        return best

    def history(self, subject: str) -> list[dict]:
        events = _state_events(self.log, subject)
        ordered = temporal_sort(events, self.log.by_id())
        projection = replay(ordered)
        state = projection.get(subject)
        if state is None:
            return []
        return [{"before": t.before, "event": t.event_id, "type": t.event_type,
                 "after": t.after, "effective_from": t.effective_from}
                for t in state.history]

    def explain_transition(self, event_id: str) -> dict:
        by_id = self.log.by_id()
        event = by_id.get(event_id)
        if event is None:
            return {"event_id": event_id, "found": False}
        return {
            "event_id": event_id,
            "found": True,
            "event_type": event.event_type,
            "subject": event.subject,
            "causal_parents": list(event.causal_parents),
            "evidence_refs": list(event.evidence_refs),
            "event_time": event.event_time,
            "recorded_at": event.recorded_at,
            "effective_from": event.effective_from,
            "note": "chronology only; causal edges are the explicit "
                    "causal_parents, never inferred from order",
        }

    # -- internals ----------------------------------------------------
    def _prov(self, state: SubjectState) -> tuple[str, ...]:
        return tuple(t.event_id for t in state.history[-3:])

    def _bag_current(self, subject: str,
                     events: list[EventEnvelope] | None = None) -> Answer:
        events = events if events is not None else _state_events(self.log, subject)
        values = [e.value for e in events
                  if e.event_type in ("STATE_CHANGED", "REVISION_MADE",
                                      "FACT_ESTABLISHED", "DECISION_MADE")
                  and e.value]
        if not values:
            return Answer(None, "UNKNOWN", "bag: no values mentioned", ())
        top, count = Counter(values).most_common(1)[0]
        if count * 2 <= len(values):
            return Answer(top, "UNCERTAIN",
                          f"bag: plurality {count}/{len(values)}; order ignored",
                          tuple(e.event_id for e in events if e.value == top))
        return Answer(top, "OK", f"bag: majority {count}/{len(values)}",
                      tuple(e.event_id for e in events if e.value == top))

    def _last_wins(self, subject: str, key: str,
                   known_at: str | None = None,
                   events: list[EventEnvelope] | None = None) -> Answer:
        events = events if events is not None else _state_events(self.log, subject)
        if known_at is not None:
            events = _known_filter(events, known_at)
        valued = [e for e in events if e.value]
        if not valued:
            return Answer(None, "UNKNOWN", f"{key}: no values", ())
        if key == "arrival":
            order = {eid: i for i, eid in enumerate(self.log.arrival_order())}
            winner = max(valued, key=lambda e: order.get(e.event_id, -1))
        else:
            winner = max(valued, key=lambda e: (e.event_time, e.event_id))
        return Answer(winner.value, "OK",
                      f"{key}-recency: {winner.event_id}", (winner.event_id,))

    def _reducer_current(self, subject: str,
                         known_at: str | None = None) -> Answer:
        events = _state_events(self.log, subject)
        if known_at is not None:
            events = _known_filter(events, known_at)
        ordered = temporal_sort(events, self.log.by_id())
        projection = replay(ordered)
        state = projection.get(subject)
        gap = " [INCOMPLETE_HISTORY: sequence gap]" if log_has_gap(self.log) else ""
        if state is None or state.current is None:
            if state is not None and state.planned is not None:
                return Answer(None, "UNKNOWN",
                              f"decided but not effective: planned={state.planned} "
                              f"from {state.planned_effective_from}{gap}",
                              self._prov(state))
            return Answer(None, "UNKNOWN", f"no state established{gap}", ())
        return Answer(state.current, "OK",
                      f"valid_from={state.valid_from}{gap}", self._prov(state))


def log_has_gap(log: EventLog) -> bool:
    return bool(log.gaps())


class MaterializedProjection:
    """Maintained current-state projection with rebuild equivalence.

    Late arrivals are handled by re-applying from the insertion point:
    the event list is kept in temporal order and the reducer replays the
    suffix from the earliest affected event. The suffix length is
    recorded so the experiment prices correction propagation honestly.
    """

    def __init__(self, projection_version: str = "temporal-projection-v0.1"):
        self.projection = empty_projection()
        self.projection_version = projection_version
        self.ingest_count = 0
        self.last_ingest: str | None = None
        self.last_suffix_len = 0
        self._events: list[EventEnvelope] = []

    def ingest(self, event: EventEnvelope, received_at: str = "") -> None:
        from .reducer import empty_projection as _empty
        self._events.append(event)
        by_id = {e.event_id: e for e in self._events}
        ordered = temporal_sort(self._events, by_id)
        idx = next(i for i, e in enumerate(ordered)
                   if e.event_id == event.event_id)
        if idx == len(ordered) - 1 and self.projection:
            reduce_into(self.projection, event)
            self.last_suffix_len = 1
        else:
            self.projection = replay(ordered)
            self.last_suffix_len = len(ordered) - idx
        self.ingest_count += 1
        self.last_ingest = received_at or event.recorded_at

    def current(self, subject: str) -> Answer:
        state = self.projection.get(subject)
        if state is None or state.current is None:
            return Answer(None, "UNKNOWN", "materialized: no state", ())
        return Answer(state.current, "OK",
                      f"materialized valid_from={state.valid_from}",
                      tuple(t.event_id for t in state.history[-3:]))

    def rebuild(self, events_in_temporal_order: list[EventEnvelope]) -> None:
        self.projection = replay(events_in_temporal_order)
        self.ingest_count = len(events_in_temporal_order)
        self._events = list(events_in_temporal_order)

    def drift_vs(self, computed: dict[str, SubjectState]) -> list[str]:
        drifted: list[str] = []
        for subject, state in computed.items():
            mine = self.projection.get(subject)
            if mine is None or mine.current != state.current:
                drifted.append(subject)
        return drifted
