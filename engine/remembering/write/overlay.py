# Bundled remembering engine (opencode-remembering product code).
"""Stage 9 relationship overlay over Stage 3 temporal annotation.

The existing temporal engine resolves explicit-memory lineage through
real envelopes (current values, valid-time and known-time standpoints).
One gap remains by design: the frozen annotator never saw explicit
lifecycle relations, so a corrected original reads as plain history
and a retracted current value still reads as current. The overlay
closes exactly that gap for memory://explicit sources, reusing Stage 3
reason semantics:

- applicable corrects -> corrected / temporal.corrected_as_current
- applicable supersedes -> superseded / temporal.superseded_as_current
- applicable retracts -> retracted / temporal.retracted_as_current
- relation not yet applicable under the standpoint -> the target
  stays the terminal-as-of-standpoint (current / temporal.current),
  which is what makes future-effective supersession and late-arriving
  correction resolve correctly.

Recall selection is untouched (recall preserves everything); influence
admission suppresses via the existing SUPPRESSING_STATUSES. Nothing
here invents a second temporal system.
"""

from __future__ import annotations

from .temporal import event_id_for, lineage_subject

EXPLICIT_PREFIX = "memory://explicit/"

_TEMPORAL_KIND = {
    "corrects": "CORRECTION_RECORDED",
    "supersedes": "STATE_CHANGED",
    "retracts": "RETRACTION_RECORDED",
}

_STATUS_FOR = {
    "corrects": ("corrected", "temporal.corrected_as_current"),
    "supersedes": ("superseded", "temporal.superseded_as_current"),
    "retracts": ("retracted", "temporal.retracted_as_current"),
}


def is_explicit_source(source_id: str) -> bool:
    return isinstance(source_id, str) and \
        source_id.startswith(EXPLICIT_PREFIX)


def record_id_of(source_id: str) -> str | None:
    if not is_explicit_source(source_id):
        return None
    record_id = source_id[len(EXPLICIT_PREFIX):]
    return record_id or None


def relation_applicable(relation: dict, standpoint, now_iso: str) -> bool:
    """A lifecycle relation applies under a standpoint when it was
    recorded in time (known axis) and is effective in time (valid
    axis), mirroring the bitemporal engine's own cutoffs."""
    from ..temporal.ordering import parse_ts

    recorded = parse_ts(relation.get("recorded_at") or "")
    known = parse_ts(
        (standpoint.known_at if standpoint is not None else None)
        or now_iso)
    if recorded is None or known is None or recorded > known:
        return False
    mode = standpoint.mode if standpoint is not None else "current"
    if mode in ("valid_at", "bitemporal"):
        valid_cutoff = (standpoint.valid_at
                        if standpoint is not None else None)
    elif mode == "current":
        valid_cutoff = now_iso
    else:  # as_known: the engine filters the known axis only
        return True
    effective = parse_ts(relation.get("effective_from")
                         or relation.get("recorded_at") or "")
    limit = parse_ts(valid_cutoff or "")
    if effective is None or limit is None:
        return False
    return effective <= limit


def apply_relationship_overlay(annotated: list, relations: list,
                               records_by_id: dict, standpoint,
                               now_iso: str) -> dict:
    """Adjust Stage 3 annotations for explicit-memory lifecycle
    relations. Mutates the given CandidateTemporal items in place and
    returns {chunk_id: applied info} for trace visibility."""
    by_target: dict[str, dict] = {}
    for relation in relations:
        target = relation.get("target_record_id")
        if not target:
            continue
        prev = by_target.get(target)
        if prev is None or str(relation.get("recorded_at") or "") >= str(
                prev.get("recorded_at") or ""):
            by_target[target] = relation
    applied: dict[str, dict] = {}
    for item in annotated:
        record_id = record_id_of(item.source_id)
        if record_id is None:
            continue
        relation = by_target.get(record_id)
        if relation is None:
            continue
        record = records_by_id.get(record_id)
        subject = lineage_subject(
            record.lineage_root if record is not None
            else record_id) if record is not None and getattr(
                record, "lineage_root", "") else None
        kind = relation.get("relation")
        if relation_applicable(relation, standpoint, now_iso):
            status_reason = _STATUS_FOR.get(kind)
            if status_reason is None:
                continue
            item.status, item.reason = status_reason
            if subject is not None:
                item.subject = subject
            item.current_event_id = event_id_for(
                relation.get("action_id") or "",
                _TEMPORAL_KIND.get(kind, "STATE_CHANGED"))
            applied[item.chunk_id] = {
                "relation": kind,
                "target_record_id": record_id,
                "applicable": True,
                "reason": item.reason,
            }
        else:
            # Not yet applicable: the target remains the terminal
            # record as of this standpoint.
            item.status = "current"
            item.reason = "temporal.current"
            if subject is not None:
                item.subject = subject
            applied[item.chunk_id] = {
                "relation": kind,
                "target_record_id": record_id,
                "applicable": False,
                "reason": "temporal.current",
            }
    return applied
