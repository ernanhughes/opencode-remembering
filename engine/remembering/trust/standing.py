# Bundled remembering engine (opencode-remembering product code).
"""Standing resolution and lineage: revocation state, restriction
state, derivation-closure revocation inheritance, disjoint-root
corroboration. Structural only — no semantic similarity, no scores."""

from __future__ import annotations

from .model import StandingEvent


def resolve_standing(events: list[StandingEvent],
                     now: str | None = None) -> dict:
    """Resolve per-source standing from append-only events.

    Returns {"revoked": {source_id}, "restricted": {source_id: [scopes]},
    "applied": [event_ids]}. Latest effective event per subject wins;
    RESTORED clears revocation, CLEARED clears restrictions.
    """
    from ..temporal.ordering import parse_ts

    cutoff = parse_ts(now) if now else None
    ordered = sorted(events, key=lambda e: (
        e.recorded_at, e.event_id))
    revoked: set[str] = set()
    restricted: dict[str, set[str]] = {}
    applied: list[str] = []
    for event in ordered:
        if cutoff is not None:
            rec = parse_ts(event.recorded_at)
            if rec is None or rec > cutoff:
                continue
        if event.subject_type != "source":
            continue
        applied.append(event.event_id)
        if event.event_type == "STANDING_REVOKED":
            revoked.add(event.subject_id)
        elif event.event_type == "STANDING_RESTORED":
            revoked.discard(event.subject_id)
        elif event.event_type == "RESTRICTION_SET":
            restricted.setdefault(event.subject_id, set()).add(
                event.scope or event.event_id)
        elif event.event_type == "RESTRICTION_CLEARED":
            restricted.pop(event.subject_id, None)
    return {"revoked": revoked,
            "restricted": {k: sorted(v) for k, v in restricted.items()},
            "applied": applied}


def lineage_roots(source_id: str,
                  derived_from: dict[str, list[str]]) -> frozenset[str]:
    """Root lineage set: ultimate ancestors (or self when underived).
    Cycles resolve to the strongly connected members, never hang."""
    roots: set[str] = set()
    stack = [source_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        parents = derived_from.get(current, [])
        if not parents:
            roots.add(current)
        else:
            stack.extend(parents)
    return frozenset(roots)


def revocation_tainted(source_id: str, revoked: set[str],
                       derived_from: dict[str, list[str]]) -> list[str]:
    """Revoked ancestors of source_id through the derivation closure
    (excluding itself; direct revocation is handled separately)."""
    tainted: list[str] = []
    stack = list(derived_from.get(source_id, []))
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in revoked:
            tainted.append(current)
        stack.extend(derived_from.get(current, []))
    return sorted(tainted)


def independent_roots(groups: dict[str, frozenset[str]]) -> bool:
    """True when every pair of root sets is disjoint: independent
    lineage. Shared-root echoes never corroborate."""
    items = list(groups.values())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i] & items[j]:
                return False
    return len(items) >= 2
