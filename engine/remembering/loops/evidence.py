# Bundled remembering engine (opencode-remembering product code).
"""Closure evidence resolution: test each requirement against
recorded evidence. No guessing: unresolvable references yield
missing (UNCERTAIN), conflicting evidence yields conflict
(UNCERTAIN), and only satisfied requirements close loops."""

from __future__ import annotations


def make_resolver(temporal=None, known_sources=None,
                  loop_events=None, source_texts=None):
    """Build a resolve(requirement, valid_at, known_at) function.

    temporal: TemporalEngine-like with current/at/bitemporal over a
    loaded log (or None when no temporal store applies).
    known_sources: set of source ids present in the project.
    loop_events: all loop events (for event_observed requirements).
    source_texts: {source_id: text} for content_contains checks.
    """
    known_sources = set(known_sources or ())
    loop_events = list(loop_events or [])
    source_texts = dict(source_texts or {})

    def resolve(requirement, valid_at=None, known_at=None) -> dict:
        kind = requirement.type
        if kind == "state_equals":
            return _resolve_state(requirement, temporal, valid_at,
                                  known_at)
        if kind == "evidence_present":
            present = requirement.source_id in known_sources
            return {
                "satisfied": present,
                "uncertain": False,
                "conflict": False,
                "missing": [] if present else [requirement.source_id],
                "detail": f"evidence_present:{requirement.source_id}:"
                          f"{'found' if present else 'missing'}",
                "search_complete": True,
            }
        if kind == "event_observed":
            matches = [e for e in loop_events
                       if e.event_type == requirement.event_type
                       and (not requirement.subject
                            or _event_subject(e) == requirement.subject)]
            if not matches:
                return {"satisfied": False, "uncertain": False,
                        "conflict": False, "missing": [],
                        "detail": f"event_observed:{requirement.event_type}:"
                                  "absent",
                        "search_complete": True}
            return {"satisfied": True, "uncertain": False,
                    "conflict": False, "missing": [],
                    "detail": f"event_observed:{requirement.event_type}:"
                              f"{matches[0].event_id}",
                    "search_complete": True}
        if kind == "content_contains":
            text = source_texts.get(requirement.source_id, "")
            if requirement.source_id not in source_texts:
                return {"satisfied": False, "uncertain": True,
                        "conflict": False,
                        "missing": [requirement.source_id],
                        "detail": f"content_contains:"
                                  f"{requirement.source_id}:unavailable",
                        "search_complete": True}
            hit = requirement.text.lower() in text.lower()
            return {"satisfied": hit, "uncertain": False,
                    "conflict": False, "missing": [],
                    "detail": f"content_contains:"
                              f"{requirement.source_id}:"
                              f"{'found' if hit else 'absent'}",
                    "search_complete": True}
        return {"satisfied": False, "uncertain": True, "conflict": False,
                "missing": [requirement.type],
                "detail": f"unknown requirement type {requirement.type}",
                "search_complete": False}

    return resolve


def _event_subject(event) -> str:
    try:
        return dict(event.payload).get("subject", "")
    except (ValueError, TypeError):
        return ""


def _resolve_state(requirement, temporal, valid_at, known_at) -> dict:
    if temporal is None:
        return {"satisfied": False, "uncertain": True, "conflict": False,
                "missing": [requirement.subject or "temporal-store"],
                "detail": "state_equals:no temporal engine available",
                "search_complete": False}
    try:
        if valid_at is not None and known_at is not None:
            answer = temporal.bitemporal(requirement.subject, valid_at,
                                         known_at)
        elif valid_at is not None:
            answer = temporal.at(requirement.subject, valid_at)
        elif known_at is not None:
            answer = temporal.as_known(requirement.subject, known_at)
        else:
            answer = temporal.current(requirement.subject)
    except Exception as exc:
        return {"satisfied": False, "uncertain": True, "conflict": False,
                "missing": [requirement.subject],
                "detail": f"state_equals:resolver error: {exc}",
                "search_complete": False}
    value = getattr(answer, "value", None)
    status = getattr(answer, "status", "UNKNOWN")
    if value is None:
        # No state established at this standpoint: the expected
        # transition has not observably occurred. That is OPEN, not
        # uncertainty — uncertainty is for unresolvable references.
        return {"satisfied": False, "uncertain": False, "conflict": False,
                "missing": [],
                "detail": f"state_equals:{requirement.subject}:"
                          "no state established",
                "search_complete": True}
    if status == "INCOMPLETE_HISTORY":
        return {"satisfied": value == requirement.value,
                "uncertain": True, "conflict": False, "missing": [],
                "detail": f"state_equals:{requirement.subject}={value}:"
                          "incomplete history",
                "search_complete": True}
    satisfied = (value == requirement.value)
    return {"satisfied": satisfied, "uncertain": False, "conflict": False,
            "missing": [],
            "detail": f"state_equals:{requirement.subject}={value}:"
                      f"{'satisfied' if satisfied else 'open'}",
            "search_complete": True}


def combine(results: list[dict], kind: str = "all") -> dict:
    """Combine per-requirement outcomes (all/any). Conflicts and
    missing refs propagate; uncertainty never silently resolves."""
    if not results:
        return {"satisfied": False, "uncertain": False, "conflict": False,
                "missing": [],
                "detail": "no closure requirements",
                "search_complete": True}
    conflict = any(r.get("conflict") for r in results)
    missing: list[str] = []
    for result in results:
        missing.extend(result.get("missing", []))
    incomplete = any(not r.get("search_complete", True) for r in results)
    satisfied_list = [bool(r.get("satisfied")) for r in results]
    if kind == "any":
        satisfied = any(satisfied_list)
    else:
        satisfied = all(satisfied_list)
    detail = "; ".join(r.get("detail", "") for r in results)
    return {"satisfied": satisfied and not conflict and not missing,
            "uncertain": incomplete or bool(missing),
            "conflict": conflict,
            "missing": missing,
            "detail": detail,
            "search_complete": not incomplete}
