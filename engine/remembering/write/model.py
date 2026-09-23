# Bundled remembering engine (opencode-remembering product code).
"""Canonical explicit-memory record and action-event model.

A memory write is an event, not an edit to the past. REMEMBER creates
one immutable record; CORRECT/SUPERSEDE create one new record plus a
lifecycle relationship; RETRACT appends a relationship/event without
replacement content. Lifecycle relationships (corrects, supersedes,
retracts) are never derivation lineage (derived_from).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import EXPLICIT_RECORD_SCHEMA, MEMORY_ACTION_SCHEMA

# Exactly these four actions exist in v0.1. No DELETE/FORGET/REWRITE.
WRITE_ACTIONS = ("remember", "correct", "supersede", "retract")
RELATION_ACTIONS = ("correct", "supersede", "retract")

ACTION_RELATION = {
    "correct": "corrects",
    "supersede": "supersedes",
    "retract": "retracts",
}

# Ordered standing classes (weakest first) for the two-key rule:
# effective = min(write ceiling, Stage 5 resolved class).
CLASS_ORDER = ("untrusted", "informational", "authoritative")

# Roles a caller may request. The write gate authorizes the requested
# role against the matching grant; it is never self-authorized.
KNOWN_ROLES = (
    "ordinary",
    "evidence",
    "proposal",
    "preference",
    "decision",
    "production_state",
)

BUILTIN_ROLES = ("ordinary", "evidence", "proposal", "preference")

# Trust-sensitive fields a caller must never set directly. They come
# from runtime attribution, write policy, existing evidence, or action
# semantics — never from model output.
FORBIDDEN_CALLER_FIELDS = (
    "source_class",
    "standing",
    "trust_verdict",
    "claim_key",
    "derived_from",
    "refuted_by",
    "corroborators",
    "dispute",
    "negative",
    "authority",
    "project_id",
    "actor_id",
)

MAX_CONTENT_CHARS = 20000


class WriteError(Exception):
    """Coded write failure (structured, never silent)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    return _sha(content)


def effective_class(policy_class: str, ceiling: str) -> str:
    """Two-key rule: the effective source class never exceeds either
    the Stage 5 trust resolution or the write-policy ceiling. A write
    policy can cap authority; it can never force Stage 5 to grant it."""
    order = {name: index for index, name in enumerate(CLASS_ORDER)}
    if policy_class not in order or ceiling not in order:
        raise WriteError("WRITE_BAD_REQUEST",
                         f"unknown source class {policy_class!r}/{ceiling!r}")
    return policy_class if order[policy_class] <= order[ceiling] \
        else ceiling


def record_id_for(project_id: str, content: str, role: str,
                  standing_ceiling: str, actor_kind: str, actor_id: str,
                  caller_scope: str, surface: str,
                  evidence_refs: tuple[str, ...],
                  effective_from: str | None) -> str:
    """Deterministic record identity over immutable semantic content
    plus attribution. The same sentence from two actors is two
    provenance records; mutable storage fields never participate."""
    canonical = json.dumps({
        "project_id": project_id,
        "content": content,
        "content_hash": content_hash(content),
        "role": role,
        "standing_ceiling": standing_ceiling,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "caller_scope": caller_scope,
        "surface": surface,
        "evidence_refs": sorted(evidence_refs),
        "effective_from": effective_from or "",
    }, sort_keys=True, ensure_ascii=False)
    return "mem_" + _sha(canonical)[:32]


def action_id_for(project_id: str, action: str,
                  new_record_id: str | None,
                  target_record_id: str | None,
                  reason: str | None, actor_kind: str, actor_id: str,
                  caller_scope: str, surface: str,
                  idempotency_key: str | None) -> str:
    """Deterministic action identity. Retries with the same stable
    host/tool-call identity collapse; different payloads under the
    same key conflict visibly instead."""
    canonical = json.dumps({
        "project_id": project_id,
        "action": action,
        "new_record_id": new_record_id or "",
        "target_record_id": target_record_id or "",
        "reason": reason or "",
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "caller_scope": caller_scope,
        "surface": surface,
        "idempotency_key": idempotency_key or "",
    }, sort_keys=True, ensure_ascii=False)
    return "mact_" + _sha(canonical)[:32]


def request_hash_for(action: str, content: str | None,
                     target_record_id: str | None, role: str | None,
                     reason: str | None,
                     evidence_refs: tuple[str, ...],
                     effective_from: str | None,
                     event_time: str | None) -> str:
    """Semantic payload digest for idempotency-conflict detection."""
    canonical = json.dumps({
        "action": action,
        "content": content or "",
        "target_record_id": target_record_id or "",
        "role": role or "",
        "reason": reason or "",
        "evidence_refs": sorted(evidence_refs),
        "effective_from": effective_from or "",
        "event_time": event_time or "",
    }, sort_keys=True, ensure_ascii=False)
    return _sha(canonical)[:32]


@dataclass(frozen=True)
class ExplicitMemoryRecord:
    record_id: str
    project_id: str
    content: str
    role: str
    standing_ceiling: str
    actor_kind: str
    actor_id: str
    caller_scope: str
    surface: str
    event_time: str | None
    recorded_at: str
    effective_from: str | None
    evidence_refs: tuple[str, ...] = ()
    lineage_root: str = ""
    created_by_action: str = ""
    content_hash: str = ""
    schema_version: str = EXPLICIT_RECORD_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs",
                           tuple(self.evidence_refs or ()))

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "project_id": self.project_id,
            "content": self.content,
            "role": self.role,
            "standing_ceiling": self.standing_ceiling,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "caller_scope": self.caller_scope,
            "surface": self.surface,
            "event_time": self.event_time,
            "recorded_at": self.recorded_at,
            "effective_from": self.effective_from,
            "evidence_refs": list(self.evidence_refs),
            "lineage_root": self.lineage_root,
            "created_by_action": self.created_by_action,
            "content_hash": self.content_hash,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ExplicitMemoryRecord":
        return cls(
            record_id=raw["record_id"],
            project_id=raw["project_id"],
            content=raw["content"],
            role=raw["role"],
            standing_ceiling=raw["standing_ceiling"],
            actor_kind=raw.get("actor_kind", ""),
            actor_id=raw.get("actor_id", ""),
            caller_scope=raw.get("caller_scope", ""),
            surface=raw.get("surface", ""),
            event_time=raw.get("event_time"),
            recorded_at=raw["recorded_at"],
            effective_from=raw.get("effective_from"),
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            lineage_root=raw.get("lineage_root", "") or "",
            created_by_action=raw.get("created_by_action", "") or "",
            content_hash=raw.get("content_hash", "") or "",
            schema_version=raw.get("schema_version",
                                   EXPLICIT_RECORD_SCHEMA),
        )

    @property
    def source_id(self) -> str:
        """Stable retrieval source namespace for this record."""
        return f"memory://explicit/{self.record_id}"


@dataclass(frozen=True)
class MemoryActionEvent:
    action_id: str
    project_id: str
    action: str
    new_record_id: str | None
    target_record_id: str | None
    relation: str | None
    reason: str | None
    actor_kind: str
    actor_id: str
    caller_scope: str
    surface: str
    event_time: str | None
    recorded_at: str
    effective_from: str | None
    evidence_refs: tuple[str, ...] = ()
    write_policy_version: str = ""
    write_policy_digest: str = ""
    matched_grant_id: str = ""
    idempotency_key: str | None = None
    request_hash: str = ""
    host_session_id: str = ""
    host_tool_call_id: str = ""
    schema_version: str = MEMORY_ACTION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs",
                           tuple(self.evidence_refs or ()))

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "project_id": self.project_id,
            "action": self.action,
            "new_record_id": self.new_record_id,
            "target_record_id": self.target_record_id,
            "relation": self.relation,
            "reason": self.reason,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "caller_scope": self.caller_scope,
            "surface": self.surface,
            "event_time": self.event_time,
            "recorded_at": self.recorded_at,
            "effective_from": self.effective_from,
            "evidence_refs": list(self.evidence_refs),
            "write_policy_version": self.write_policy_version,
            "write_policy_digest": self.write_policy_digest,
            "matched_grant_id": self.matched_grant_id,
            "idempotency_key": self.idempotency_key,
            "request_hash": self.request_hash,
            "host_session_id": self.host_session_id,
            "host_tool_call_id": self.host_tool_call_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MemoryActionEvent":
        return cls(
            action_id=raw["action_id"],
            project_id=raw["project_id"],
            action=raw["action"],
            new_record_id=raw.get("new_record_id"),
            target_record_id=raw.get("target_record_id"),
            relation=raw.get("relation"),
            reason=raw.get("reason"),
            actor_kind=raw.get("actor_kind", ""),
            actor_id=raw.get("actor_id", ""),
            caller_scope=raw.get("caller_scope", ""),
            surface=raw.get("surface", ""),
            event_time=raw.get("event_time"),
            recorded_at=raw["recorded_at"],
            effective_from=raw.get("effective_from"),
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            write_policy_version=raw.get("write_policy_version", ""),
            write_policy_digest=raw.get("write_policy_digest", ""),
            matched_grant_id=raw.get("matched_grant_id", ""),
            idempotency_key=raw.get("idempotency_key"),
            request_hash=raw.get("request_hash", "") or "",
            host_session_id=raw.get("host_session_id", "") or "",
            host_tool_call_id=raw.get("host_tool_call_id", "") or "",
            schema_version=raw.get("schema_version",
                                   MEMORY_ACTION_SCHEMA),
        )


def _iso_or_none(label: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WriteError("WRITE_BAD_TIMESTAMP",
                         f"{label} must be ISO-8601 or omitted")
    from ..temporal.ordering import parse_ts

    if parse_ts(value.strip()) is None:
        raise WriteError("WRITE_BAD_TIMESTAMP",
                         f"{label}={value!r} is not ISO-8601")
    return value.strip()


def validate_request(raw: dict) -> dict:
    """Validate a memory_remember payload. Returns a normalized
    request dict. Raises WriteError with a stable code. Content is
    never screened for truth, malice, or importance — only shape,
    size, and caller-forged authority metadata are checked."""
    if not isinstance(raw, dict):
        raise WriteError("WRITE_BAD_REQUEST",
                         "memory_remember input must be an object")
    for forbidden in FORBIDDEN_CALLER_FIELDS:
        if forbidden in raw:
            raise WriteError(
                "WRITE_FORBIDDEN_METADATA",
                f"caller must not set trust-sensitive field {forbidden!r}; "
                "it comes from runtime attribution and write policy")
    action = raw.get("action", "remember")
    if not isinstance(action, str):
        raise WriteError("WRITE_BAD_REQUEST", "action must be a string")
    action = action.strip().lower()
    if action not in WRITE_ACTIONS:
        raise WriteError("WRITE_BAD_REQUEST",
                         f"unknown action {action!r}: expected one of "
                         f"{WRITE_ACTIONS}")

    content = raw.get("content")
    if action in ("remember", "correct", "supersede"):
        if not isinstance(content, str) or not content.strip():
            raise WriteError("WRITE_EMPTY_CONTENT",
                             f"action {action!r} requires non-empty content")
        if len(content) > MAX_CONTENT_CHARS:
            raise WriteError(
                "WRITE_CONTENT_TOO_LARGE",
                f"content is {len(content)} chars; the explicit-memory "
                f"maximum is {MAX_CONTENT_CHARS} (not truncated)")
        content = content.strip()
    else:
        if content is not None and (
                not isinstance(content, str) or len(content) >
                MAX_CONTENT_CHARS):
            raise WriteError("WRITE_CONTENT_TOO_LARGE",
                             "retract content exceeds the maximum")

    target = raw.get("target_record_id")
    if action == "remember":
        if target is not None and (
                not isinstance(target, str) or target.strip()):
            raise WriteError("WRITE_BAD_REQUEST",
                             "remember creates a new record and takes no "
                             "target_record_id")
        target = None
    else:
        if not isinstance(target, str) or not target.strip():
            raise WriteError("WRITE_BAD_REQUEST",
                             f"action {action!r} requires target_record_id")
        target = target.strip()

    role = raw.get("role", "ordinary")
    if not isinstance(role, str) or not role.strip():
        raise WriteError("WRITE_BAD_REQUEST", "role must be a string")
    role = role.strip().lower()
    if role not in KNOWN_ROLES:
        raise WriteError("WRITE_BAD_REQUEST",
                         f"unknown role {role!r}: expected one of "
                         f"{KNOWN_ROLES}")

    reason = raw.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise WriteError("WRITE_BAD_REQUEST",
                             "reason must be a string")
        reason = reason.strip() or None
    if action == "retract" and not reason:
        raise WriteError("WRITE_REASON_REQUIRED",
                         "retract changes current applicability without "
                         "replacement content and requires a reason")

    evidence_refs = raw.get("evidence_refs", [])
    if not isinstance(evidence_refs, list) or not all(
            isinstance(v, str) and v.strip() for v in evidence_refs):
        raise WriteError("WRITE_BAD_REQUEST",
                         "evidence_refs must be a list of strings")
    evidence_refs = tuple(r.strip() for r in evidence_refs)

    idempotency_key = raw.get("idempotency_key")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or \
                not idempotency_key.strip():
            raise WriteError("WRITE_BAD_REQUEST",
                             "idempotency_key must be a non-empty string")
        idempotency_key = idempotency_key.strip()

    return {
        "action": action,
        "content": content,
        "target_record_id": target,
        "role": role,
        "reason": reason,
        "evidence_refs": evidence_refs,
        "effective_from": _iso_or_none("effective_from",
                                       raw.get("effective_from")),
        "event_time": _iso_or_none("event_time", raw.get("event_time")),
        "idempotency_key": idempotency_key,
    }
