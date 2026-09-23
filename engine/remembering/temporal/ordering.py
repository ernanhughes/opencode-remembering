# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""Ordering utilities: partial orders, interval relations, gap detection.

Only the Allen subset the fixtures earn is implemented: before, meets,
overlaps, during, equals — plus bounded-unknown interval overlap tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .model import EventEnvelope


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -- Allen subset ----------------------------------------------------

def allen_relation(a_from: str, a_to: str | None,
                   b_from: str, b_to: str | None) -> str:
    """Classify the relation of interval A to interval B (open end = +inf)."""
    a0, b0 = parse_ts(a_from), parse_ts(b_from)
    a1, b1 = parse_ts(a_to), parse_ts(b_to)
    if a0 is None or b0 is None:
        return "unknown"
    if a0 == b0 and a_to == b_to:
        return "equals"
    a1_inf = a1 is None
    b1_inf = b1 is None
    if not a1_inf and not b1_inf:
        assert a1 is not None and b1 is not None
        if a1 < b0:
            return "before"
        if a1 == b0:
            return "meets"
        if b1 < a0:
            return "after"
        if b1 == a0:
            return "met-by"
        if a0 < b0 < a1 < b1:
            return "overlaps"
        if b0 < a0 < b1 < a1:
            return "overlapped-by"
        if (a0 > b0 and a1 < b1) or (a0 == b0 and a1 < b1) \
                or (a0 > b0 and a1 == b1):
            return "during"
        if (b0 > a0 and b1 < a1) or (b0 == a0 and b1 < a1) \
                or (b0 > a0 and b1 == a1):
            return "includes"
        return "overlaps"
    if a1_inf and b1_inf:
        if a0 < b0:
            return "before-or-overlaps"
        if a0 > b0:
            return "after-or-overlapped-by"
        return "equals"
    if a1_inf:
        assert b1 is not None
        if a0 >= b1:
            return "after"
        return "overlaps-or-during"
    assert a1 is not None
    if a1 <= b0:
        return "before" if a1 < b0 else "meets"
    return "overlaps-or-during"


def intervals_overlap(a_from: str, a_to: str | None,
                      b_from: str, b_to: str | None) -> bool:
    """True unless the intervals are provably disjoint (honest uncertainty)."""
    rel = allen_relation(a_from, a_to, b_from, b_to)
    return rel not in ("before", "after", "meets", "met-by")


# -- partial order ----------------------------------------------------

def known_before(a: EventEnvelope, b: EventEnvelope,
                 by_id: dict[str, EventEnvelope]) -> bool | None:
    """True if a is known to precede b; False if b precedes a; None if
    their relative order is unknown (concurrent). Explicit causal edges
    dominate wall-clock comparison."""
    if a.event_id in b.causal_parents:
        return True
    if b.event_id in a.causal_parents:
        return False
    ta, tb = parse_ts(a.event_time), parse_ts(b.event_time)
    if ta is None or tb is None:
        return None
    if ta < tb:
        return True
    if tb < ta:
        return False
    return None  # equal timestamps, no causal edge: concurrent


def temporal_sort(events: list[EventEnvelope],
                  by_id: dict[str, EventEnvelope]) -> list[EventEnvelope]:
    """Deterministic topological sort: causal parents always precede
    children (Kahn's algorithm); among ready events, event time then
    event id wins. Concurrent events keep id order (stable, and the
    resolver must not read meaning into it)."""
    ids = {e.event_id for e in events}
    children: dict[str, list[str]] = {e.event_id: [] for e in events}
    indegree: dict[str, int] = {e.event_id: 0 for e in events}
    for event in events:
        for parent in event.causal_parents:
            if parent in ids:
                children[parent].append(event.event_id)
                indegree[event.event_id] += 1
    import heapq
    event_by_id = {e.event_id: e for e in events}
    heap = [(e.event_time, e.event_id) for e in events
            if indegree[e.event_id] == 0]
    heapq.heapify(heap)
    ordered: list[EventEnvelope] = []
    while heap:
        _, eid = heapq.heappop(heap)
        ordered.append(event_by_id[eid])
        for child in children[eid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                child_event = event_by_id[child]
                heapq.heappush(heap, (child_event.event_time,
                                      child_event.event_id))
    if len(ordered) != len(events):
        # Causal cycle: fall back to pure time order (flagged in health).
        return sorted(events, key=lambda e: (e.event_time, e.event_id))
    return ordered


# -- temporal admissibility (Ch7 x Ch8) --------------------------------

def admissible_role(evidence_time: str, decision_time: str,
                    role: str) -> tuple[bool, str]:
    """Could this evidence have played this role for a past decision?

    MOTIVATED_BY requires evidence before (or at) the decision.
    CORROBORATED_BY may come later. CORRECTED_BY must come later in
    transaction time. Unknown relation kinds abstain (return True with a
    note) rather than inventing constraints.
    """
    role = role.upper()
    ev, dec = parse_ts(evidence_time), parse_ts(decision_time)
    if ev is None or dec is None:
        return True, "unknown timestamps: cannot rule out"
    if role in ("MOTIVATED_BY", "ANTECEDENT", "SUPPORTED_DECISION"):
        if ev <= dec:
            return True, "evidence precedes decision: admissible antecedent"
        return False, ("evidence is later than the decision: cannot have "
                       "motivated it; corroboration only")
    if role in ("CORROBORATED_BY", "SUPPORTS_PROPOSITION"):
        return True, "corroboration has no precedence requirement"
    if role in ("CORRECTED_BY", "SUPERSEDED_BY"):
        if ev >= dec:
            return True, "correction/succession follows the original"
        return False, "correction cannot precede its target"
    return True, f"unknown role {role}: no constraint applied"
