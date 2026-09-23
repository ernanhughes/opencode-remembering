# Bundled remembering engine (opencode-remembering product code).
"""Explicit-memory write orchestration: validate, authorize, embed,
persist atomically.

The required order is load-bearing: embedding (the external,
fallible operation) happens BEFORE any database write, and all
database effects land in ONE transaction. A successful write is
therefore durably stored AND immediately retrievable; any failure
leaves no partial write.

Dependencies are injected as a dict so the same orchestration runs
against PostgreSQL (bridge) and recording fakes (evaluation):

- load_policy() -> {"policy", "configured", "valid", "error"}
- now() -> ISO-8601 string
- get_record(record_id) -> ExplicitMemoryRecord | None
- successors(record_id) -> list[relation dict]
- get_action(action_id) -> MemoryActionEvent | None
- get_action_by_key(idempotency_key) -> MemoryActionEvent | None
- evidence_exists(ref) -> bool
- next_source_seq(source_id) -> int
- embed(texts) -> {"vectors": [[float]], "version": str}
- transact(body) -> body result (commits; rolls back on error)
- put_record(record), put_action(event, received_at),
  put_relation(source, target, relation, action_id, effective,
  recorded), put_temporal(envelope), put_chunks(source_row,
  chunk_rows)
"""

from __future__ import annotations

import time

from . import WRITE_GATE_VERSION
from .gate import authorize
from .model import (ACTION_RELATION, WriteError, action_id_for,
                    content_hash, effective_class, record_id_for,
                    request_hash_for, validate_request)
from .temporal import envelope_for_action

DENY_CODE = {
    "write.deny.policy_invalid": "WRITE_POLICY_INVALID",
    "write.deny.action_not_allowed": "WRITE_ACTION_NOT_ALLOWED",
    "write.deny.role_not_allowed": "WRITE_ROLE_NOT_ALLOWED",
    "write.deny.relationship_requires_grant":
        "WRITE_RELATIONSHIP_DENIED",
    "write.deny.target_not_found": "WRITE_TARGET_NOT_FOUND",
    "write.deny.cross_project": "WRITE_PROJECT_MISMATCH",
    "write.deny.target_class": "WRITE_TARGET_CLASS_DENIED",
    "write.deny.relation_conflict": "WRITE_RELATION_CONFLICT",
    "write.deny.backdating_not_allowed":
        "WRITE_BACKDATING_NOT_ALLOWED",
    "write.deny.forbidden_metadata": "WRITE_FORBIDDEN_METADATA",
}

_CHUNKER = "explicit-memory-v0.1"


def chunk_id_for(source_id: str, text: str) -> str:
    import hashlib

    inner = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return hashlib.sha1(
        f"{source_id}|{_CHUNKER}|0|{inner}".encode("utf-8")
    ).hexdigest()[:16]


def _fail(code: str, reason: str, latencies: dict,
          **extra) -> dict:
    return {"ok": False, "code": code, "reason": reason,
            "latencies_ms": dict(latencies), **extra}


def perform_write(deps: dict, raw: dict, attribution: dict) -> dict:
    """Execute one memory_remember request. Returns a structured
    result (never raises for authorization/data outcomes)."""
    latencies: dict[str, float] = {}
    total_started = time.perf_counter()
    project_id = attribution.get("project_id", "")
    surface = attribution.get("surface", "opencode")
    caller_scope = attribution.get("caller_scope", "default") or \
        "default"
    actor_kind = attribution.get("actor_kind", "agent")
    actor_id = attribution.get("actor_id", "")
    host_key = attribution.get("idempotency_key") or \
        attribution.get("host_tool_call_id")

    started = time.perf_counter()
    try:
        request = validate_request(raw)
    except WriteError as exc:
        latencies["validation"] = round(
            (time.perf_counter() - started) * 1000, 2)
        latencies["total"] = round(
            (time.perf_counter() - total_started) * 1000, 2)
        return _fail(exc.code, "write.deny.forbidden_metadata"
                     if exc.code == "WRITE_FORBIDDEN_METADATA"
                     else "write.error.bad_request", latencies,
                     action=request_action(raw))
    latencies["validation"] = round(
        (time.perf_counter() - started) * 1000, 2)
    if request.get("idempotency_key") is None and host_key:
        request = {**request, "idempotency_key": host_key}

    started = time.perf_counter()
    loaded = deps["load_policy"]()
    policy = loaded["policy"]
    now_iso = deps["now"]()
    request = {**request, "project_id": project_id}
    target = None
    if request["action"] in ("correct", "supersede", "retract"):
        target = deps["get_record"](request["target_record_id"])
    has_successor = bool(
        deps["successors"](request["target_record_id"])
        if target is not None else False)
    decision = authorize(policy, loaded["valid"], request, surface,
                         caller_scope, target, has_successor, now_iso)
    latencies["authorization"] = round(
        (time.perf_counter() - started) * 1000, 2)
    auth_block = {
        "verdict": decision["verdict"],
        "reason": decision["reason"],
        "policy_version": policy.version,
        "policy_digest": policy.digest,
        "matched_grant_id": decision["matched_grant_id"],
        "gate_version": WRITE_GATE_VERSION,
    }
    if decision["verdict"] != "allow":
        latencies["total"] = round(
            (time.perf_counter() - total_started) * 1000, 2)
        return _fail(DENY_CODE.get(decision["reason"],
                                   "WRITE_DENIED"),
                     decision["reason"], latencies,
                     action=request["action"],
                     target_record_id=request["target_record_id"],
                     authorization=auth_block)
    ceiling = decision["ceiling"]

    for ref in request["evidence_refs"]:
        if not deps["evidence_exists"](ref):
            latencies["total"] = round(
                (time.perf_counter() - total_started) * 1000, 2)
            return _fail("WRITE_EVIDENCE_REF_NOT_FOUND",
                         "write.error.evidence_ref_not_found",
                         latencies, action=request["action"],
                         authorization=auth_block,
                         evidence_ref=ref)
    request_hash = request_hash_for(
        request["action"], request.get("content"),
        request.get("target_record_id"), request.get("role"),
        request.get("reason"), request["evidence_refs"],
        request.get("effective_from"), request.get("event_time"))

    # Idempotent retries collapse on the stable key; a reused key
    # with different semantics fails visibly.
    if request["idempotency_key"]:
        existing = deps["get_action_by_key"](
            request["idempotency_key"])
        if existing is not None:
            latencies["total"] = round(
                (time.perf_counter() - total_started) * 1000, 2)
            if existing.request_hash == request_hash and \
                    existing.project_id == project_id:
                return _duplicate(existing, deps, auth_block,
                                  latencies)
            return _fail("WRITE_IDEMPOTENCY_CONFLICT",
                         "write.error.idempotency_conflict",
                         latencies, action=request["action"],
                         authorization=auth_block,
                         conflicting_action_id=existing.action_id)

    from .model import ExplicitMemoryRecord, MemoryActionEvent

    record = None
    if request["action"] != "retract":
        lineage_root = (target.lineage_root or target.record_id) \
            if target is not None else None
        record_id = record_id_for(
            project_id, request["content"], request["role"],
            ceiling, actor_kind, actor_id, caller_scope, surface,
            request["evidence_refs"],
            request.get("effective_from"))
        if lineage_root is None:
            lineage_root = record_id
        record = ExplicitMemoryRecord(
            record_id=record_id,
            project_id=project_id,
            content=request["content"],
            role=request["role"],
            standing_ceiling=ceiling,
            actor_kind=actor_kind,
            actor_id=actor_id,
            caller_scope=caller_scope,
            surface=surface,
            event_time=request.get("event_time"),
            recorded_at=now_iso,
            effective_from=request.get("effective_from"),
            evidence_refs=request["evidence_refs"],
            lineage_root=lineage_root,
            created_by_action="",
            content_hash=content_hash(request["content"]),
        )
    action_id = action_id_for(
        project_id, request["action"],
        record.record_id if record is not None else None,
        request.get("target_record_id"), request.get("reason"),
        actor_kind, actor_id, caller_scope, surface,
        request["idempotency_key"])
    prior = deps["get_action"](action_id)
    if prior is not None:
        latencies["total"] = round(
            (time.perf_counter() - total_started) * 1000, 2)
        if prior.request_hash == request_hash and \
                prior.project_id == project_id:
            return _duplicate(prior, deps, auth_block, latencies)
        return _fail("WRITE_IDEMPOTENCY_CONFLICT",
                     "write.error.idempotency_conflict", latencies,
                     action=request["action"],
                     authorization=auth_block,
                     conflicting_action_id=prior.action_id)
    if record is not None:
        record = ExplicitMemoryRecord(
            **{**record.to_dict(), "created_by_action": action_id})
    relation = ACTION_RELATION.get(request["action"])
    event = MemoryActionEvent(
        action_id=action_id,
        project_id=project_id,
        action=request["action"],
        new_record_id=record.record_id if record is not None
        else None,
        target_record_id=request.get("target_record_id"),
        relation=relation,
        reason=request.get("reason"),
        actor_kind=actor_kind,
        actor_id=actor_id,
        caller_scope=caller_scope,
        surface=surface,
        event_time=request.get("event_time"),
        recorded_at=now_iso,
        effective_from=request.get("effective_from"),
        evidence_refs=request["evidence_refs"],
        write_policy_version=policy.version,
        write_policy_digest=policy.digest,
        matched_grant_id=decision["matched_grant_id"] or "",
        idempotency_key=request["idempotency_key"],
        request_hash=request_hash,
        host_session_id=attribution.get("host_session_id", "") or "",
        host_tool_call_id=attribution.get("host_tool_call_id", "")
        or "",
    )

    # External embedding BEFORE the transaction: no half write.
    vectors: list[list[float]] = []
    embedded_version = ""
    latencies["embedding"] = 0.0
    if record is not None:
        started = time.perf_counter()
        try:
            produced = deps["embed"]([record.content])
            vectors = [list(v) for v in produced["vectors"]]
            embedded_version = produced["version"]
        except Exception as exc:
            latencies["embedding"] = round(
                (time.perf_counter() - started) * 1000, 2)
            latencies["total"] = round(
                (time.perf_counter() - total_started) * 1000, 2)
            return _fail("WRITE_INDEX_FAILED",
                         "write.error.index_failed", latencies,
                         action=request["action"],
                         authorization=auth_block,
                         detail=f"{type(exc).__name__}:"
                                f"{str(exc)[:160]}")
        latencies["embedding"] = round(
            (time.perf_counter() - started) * 1000, 2)
        if not vectors or not vectors[0]:
            latencies["total"] = round(
                (time.perf_counter() - total_started) * 1000, 2)
            return _fail("WRITE_INDEX_FAILED",
                         "write.error.index_failed", latencies,
                         action=request["action"],
                         authorization=auth_block,
                         detail="embedder returned no vector")

    prior_event_id = None
    if target is not None and request["action"] in (
            "correct", "supersede"):
        creating = deps["get_action"](target.created_by_action) \
            if target.created_by_action else None
        prior_event_id = target_event_id(target, creating)
    if request["action"] == "retract" and target is not None:
        source_id = target.source_id
    elif record is not None:
        source_id = record.source_id
    else:  # pragma: no cover - defensive; gate rejects missing target
        latencies["total"] = round(
            (time.perf_counter() - total_started) * 1000, 2)
        return _fail("WRITE_TARGET_NOT_FOUND",
                     "write.deny.target_not_found", latencies,
                     action=request["action"],
                     authorization=auth_block)
    seq = 1 if record is not None else deps["next_source_seq"](
        source_id)
    envelope = envelope_for_action(
        record, event,
        record.lineage_root if record is not None
        else (target.lineage_root if target is not None else ""),
        prior_event_id, seq)

    chunk_rows: list[tuple] = []
    source_row: tuple | None = None
    if record is not None:
        chunk_id = chunk_id_for(record.source_id, record.content)
        source_row = (record.source_id, "explicit-memory",
                      record.content_hash, record.recorded_at)
        chunk_rows = [(
            chunk_id, record.source_id, 0, record.content, None,
            0, len(record.content), record.content_hash, _CHUNKER,
            embedded_version, vectors[0],
        )]

    started = time.perf_counter()
    try:
        def _body(store) -> None:
            if record is not None:
                store["put_record"](record)
            store["put_action"](event, now_iso)
            if relation is not None and \
                    request.get("target_record_id"):
                source_ref = record.record_id \
                    if record is not None else action_id
                store["put_relation"](
                    source_ref, request["target_record_id"],
                    relation, action_id,
                    request.get("effective_from") or now_iso,
                    now_iso)
            store["put_temporal"](envelope)
            if source_row is not None:
                store["put_chunks"](source_row, chunk_rows)

        deps["transact"](_body)
    except Exception as exc:
        latencies["transaction"] = round(
            (time.perf_counter() - started) * 1000, 2)
        latencies["total"] = round(
            (time.perf_counter() - total_started) * 1000, 2)
        return _fail("WRITE_STORE_FAILED", "write.error.store_failed",
                     latencies, action=request["action"],
                     authorization=auth_block,
                     detail=f"{type(exc).__name__}:{str(exc)[:160]}")
    latencies["transaction"] = round(
        (time.perf_counter() - started) * 1000, 2)
    latencies["total"] = round(
        (time.perf_counter() - total_started) * 1000, 2)
    return {
        "ok": True,
        "action_id": action_id,
        "action": request["action"],
        "record_id": record.record_id if record is not None
        else None,
        "target_record_id": request.get("target_record_id"),
        "duplicate": False,
        "authorization": auth_block,
        "record": None if record is None else {
            "role": record.role,
            "standing_ceiling": record.standing_ceiling,
            "source_id": record.source_id,
            "lineage_root": record.lineage_root,
        },
        "index": {"indexed": record is not None,
                  "chunks": len(chunk_rows),
                  "embedded": len(chunk_rows)},
        "relation": None if relation is None else {
            "type": relation,
            "target_record_id": request["target_record_id"],
        },
        "latencies_ms": dict(latencies),
    }


def _duplicate(event, deps: dict, auth_block: dict,
               latencies: dict) -> dict:
    record = None
    if event.new_record_id:
        record = deps["get_record"](event.new_record_id)
    return {
        "ok": True,
        "action_id": event.action_id,
        "action": event.action,
        "record_id": event.new_record_id,
        "target_record_id": event.target_record_id,
        "duplicate": True,
        "authorization": auth_block,
        "record": None if record is None else {
            "role": record.role,
            "standing_ceiling": record.standing_ceiling,
            "source_id": record.source_id,
            "lineage_root": record.lineage_root,
        },
        "index": {"indexed": record is not None,
                  "chunks": 1 if record is not None else 0,
                  "embedded": 1 if record is not None else 0},
        "relation": None if not event.relation else {
            "type": event.relation,
            "target_record_id": event.target_record_id,
        },
        "latencies_ms": dict(latencies),
    }


def request_action(raw: dict) -> str | None:
    action = raw.get("action", "remember") if isinstance(
        raw, dict) else None
    return action if isinstance(action, str) else None


def target_event_id(target, creating) -> str | None:
    """Temporal event id established by the target record's own
    creating action (deterministic, no log scan needed)."""
    from .temporal import event_id_for as _eid

    if target is None or not target.created_by_action:
        return None
    kind_by_action = {
        "remember": "FACT_ESTABLISHED",
        "correct": "CORRECTION_RECORDED",
        "supersede": "STATE_CHANGED",
    }
    action = creating.action if creating is not None else "remember"
    kind = kind_by_action.get(action)
    if kind is None:
        return None
    return _eid(target.created_by_action, kind)
