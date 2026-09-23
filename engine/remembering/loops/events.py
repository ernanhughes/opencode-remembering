# Bundled remembering engine (opencode-remembering product code).
"""Loop event validation and construction. Strict, idempotent,
append-only: exact replays are no-ops, conflicting identity reuse
fails as LOOP_EVENT_CONFLICT, malformed lines fail visibly. TODO
text, suggestions, and discussion prose never create loops — only
explicit structured events do."""

from __future__ import annotations

from .model import (
    LOOP_EVENT_SCHEMA,
    LOOP_EVENT_TYPES,
    OPEN_LOOP_SCHEMA,
    ClosureRequirement,
    ExpectedTransition,
    OpenLoopEvent,
    TRANSITION_KINDS,
)


def _parse_ts(value: str | None):
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_transition_dict(raw: dict) -> ExpectedTransition:
    if not isinstance(raw, dict):
        raise ValueError("LOOP_EVENT_INVALID:transition must be an object")
    for field in ("loop_id", "subject", "transition_kind"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"LOOP_EVENT_INVALID:{field} must be set")
    kind = raw["transition_kind"].strip()
    if kind not in TRANSITION_KINDS:
        raise ValueError(
            f"LOOP_EVENT_INVALID:unknown transition_kind {kind!r}")
    closure_kind = raw.get("closure_kind", "all")
    if closure_kind not in ("all", "any"):
        raise ValueError(
            "LOOP_EVENT_INVALID:closure_kind must be 'all' or 'any'")
    closure_raw = raw.get("closure", [])
    if not isinstance(closure_raw, list):
        raise ValueError("LOOP_EVENT_INVALID:closure must be a list")
    closure = tuple(ClosureRequirement.from_dict(item)
                    for item in closure_raw)
    evidence = raw.get("evidence_refs", [])
    if not isinstance(evidence, list) or not all(
            isinstance(v, str) for v in evidence):
        raise ValueError("LOOP_EVENT_INVALID:evidence_refs must be strings")
    created_at = raw.get("created_at", "")
    if created_at and _parse_ts(created_at) is None:
        raise ValueError(
            f"LOOP_EVENT_INVALID:bad created_at {created_at!r}")
    effective = raw.get("effective_from", "")
    if effective and _parse_ts(effective) is None:
        raise ValueError(
            f"LOOP_EVENT_INVALID:bad effective_from {effective!r}")
    return ExpectedTransition(
        loop_id=raw["loop_id"].strip(),
        project_id=str(raw.get("project_id", "") or "").strip(),
        subject=raw["subject"].strip(),
        transition_kind=kind,
        from_state=str(raw.get("from_state", "") or ""),
        expected_state=str(raw.get("expected_state", "") or ""),
        created_at=created_at,
        effective_from=effective,
        evidence_refs=tuple(evidence),
        supersedes_loop_id=str(raw.get("supersedes_loop_id", "") or ""),
        closure_kind=closure_kind,
        closure=closure,
        schema_version=raw.get("schema_version", OPEN_LOOP_SCHEMA),
    )


def validate_event(event: OpenLoopEvent,
                   known_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if not event.event_id:
        errors.append("EMPTY_EVENT_ID")
    if event.event_id in known_ids:
        errors.append(f"DUPLICATE_EVENT_ID:{event.event_id}")
    if event.event_type not in LOOP_EVENT_TYPES:
        errors.append(f"UNKNOWN_EVENT_TYPE:{event.event_type}")
    if not event.loop_id:
        errors.append("EMPTY_LOOP_ID")
    if _parse_ts(event.event_time) is None:
        errors.append(f"BAD_EVENT_TIME:{event.event_time}")
    if _parse_ts(event.recorded_at) is None:
        errors.append(f"BAD_RECORDED_AT:{event.recorded_at}")
    if event.schema_version != LOOP_EVENT_SCHEMA:
        errors.append(f"UNSUPPORTED_SCHEMA:{event.schema_version}")
    if event.event_type == "LOOP_CREATED":
        try:
            transition_from_created(event)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"LOOP_EVENT_INVALID:bad transition: {exc}")
    return errors


def transition_from_created(event: OpenLoopEvent) -> ExpectedTransition:
    payload = {k: v for k, v in event.payload}
    closure = payload.get("closure", [])
    if isinstance(closure, str):
        import json

        closure = json.loads(closure)
    return validate_transition_dict({
        "loop_id": event.loop_id,
        "project_id": event.project_id,
        "subject": payload.get("subject", ""),
        "transition_kind": payload.get("transition_kind", "TASK"),
        "from_state": payload.get("from_state", ""),
        "expected_state": payload.get("expected_state", ""),
        "created_at": event.event_time,
        "effective_from": payload.get("effective_from", ""),
        "evidence_refs": list(event.evidence_refs),
        "supersedes_loop_id": payload.get("supersedes_loop_id", ""),
        "closure_kind": payload.get("closure_kind", "all"),
        "closure": closure,
    })
