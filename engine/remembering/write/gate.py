# Bundled remembering engine (opencode-remembering product code).
"""Deterministic write gate: may this caller append this memory action?

The gate answers ALLOW or DENY with a stable reason code. It never
asks a model, never scores authority, and never screens content for
truth or usefulness. A caller may legitimately preserve "skip
validation" as history; the gate controls who may write, which action
and role they may use, which standing ceiling the record receives,
which records they may target, and whether they may backdate.

WRITE_ALLOWED is never TRUST_ADMITTED: the gate assigns at most a
standing ceiling. Stage 5 independently decides influence.
"""

from __future__ import annotations

from . import WRITE_GATE_VERSION
from .model import ACTION_RELATION, RELATION_ACTIONS
from .policy import WriteGrant, WritePolicy, match_grant

# Gate reason vocabulary (stable, asserted in tests).
R_ALLOW_BUILTIN = "write.allow.builtin_untrusted_remember"
R_ALLOW_GRANT = "write.allow.policy_grant"
R_DENY_POLICY_INVALID = "write.deny.policy_invalid"
R_DENY_ACTION = "write.deny.action_not_allowed"
R_DENY_ROLE = "write.deny.role_not_allowed"
R_DENY_RELATIONSHIP = "write.deny.relationship_requires_grant"
R_DENY_TARGET = "write.deny.target_not_found"
R_DENY_CROSS_PROJECT = "write.deny.cross_project"
R_DENY_TARGET_CLASS = "write.deny.target_class"
R_DENY_BACKDATING = "write.deny.backdating_not_allowed"
R_DENY_CONFLICT = "write.deny.relation_conflict"
R_DENY_FORBIDDEN = "write.deny.forbidden_metadata"
R_ERR_EVIDENCE = "write.error.evidence_ref_not_found"
R_ERR_INDEX = "write.error.index_failed"
R_ERR_IDEMPOTENCY = "write.error.idempotency_conflict"
R_ERR_STORE = "write.error.store_failed"


def _is_backdate(effective_from: str | None, now_iso: str) -> bool:
    """An effective_from earlier than the write's recorded time
    rewrites the apparent timeline and needs a separate grant."""
    if not effective_from:
        return False
    from ..temporal.ordering import parse_ts

    want = parse_ts(effective_from)
    now = parse_ts(now_iso)
    if want is None or now is None:
        return False
    return want < now


def authorize(policy: WritePolicy, policy_valid: bool, request: dict,
              surface: str, caller_scope: str, target_record,
              target_has_successor: bool,
              now_iso: str) -> dict:
    """Pure gate decision. target_record is an ExplicitMemoryRecord or
    None (relation actions only); target_has_successor reports whether
    the target already has a lifecycle successor (branch guard)."""
    action = request["action"]
    if not policy_valid:
        return {"verdict": "deny", "reason": R_DENY_POLICY_INVALID,
                "ceiling": None, "matched_grant_id": None,
                "gate_version": WRITE_GATE_VERSION}
    grant = match_grant(policy, surface, caller_scope, action)
    if grant is None:
        if action in RELATION_ACTIONS:
            return {"verdict": "deny",
                    "reason": R_DENY_RELATIONSHIP, "ceiling": None,
                    "matched_grant_id": None,
                    "gate_version": WRITE_GATE_VERSION}
        return {"verdict": "deny", "reason": R_DENY_ACTION,
                "ceiling": None, "matched_grant_id": None,
                "gate_version": WRITE_GATE_VERSION}
    if request["role"] not in grant.roles:
        # Never silently downgrade the requested role.
        return {"verdict": "deny", "reason": R_DENY_ROLE,
                "ceiling": None, "matched_grant_id": grant.id,
                "gate_version": WRITE_GATE_VERSION}
    if action in RELATION_ACTIONS:
        if target_record is None:
            return {"verdict": "deny", "reason": R_DENY_TARGET,
                    "ceiling": None, "matched_grant_id": grant.id,
                    "gate_version": WRITE_GATE_VERSION}
        if target_record.project_id != request["project_id"]:
            return {"verdict": "deny",
                    "reason": R_DENY_CROSS_PROJECT, "ceiling": None,
                    "matched_grant_id": grant.id,
                    "gate_version": WRITE_GATE_VERSION}
        if target_record.standing_ceiling not in grant.target_classes:
            return {"verdict": "deny",
                    "reason": R_DENY_TARGET_CLASS, "ceiling": None,
                    "matched_grant_id": grant.id,
                    "gate_version": WRITE_GATE_VERSION}
        if target_has_successor:
            # v0.1 keeps replacement a deterministic chain: relation
            # actions must target the currently active terminal record.
            return {"verdict": "deny", "reason": R_DENY_CONFLICT,
                    "ceiling": None, "matched_grant_id": grant.id,
                    "gate_version": WRITE_GATE_VERSION}
    if _is_backdate(request.get("effective_from"), now_iso) and \
            not grant.allow_backdating:
        return {"verdict": "deny", "reason": R_DENY_BACKDATING,
                "ceiling": None, "matched_grant_id": grant.id,
                "gate_version": WRITE_GATE_VERSION}
    builtin = policy.policy_source == "builtin_default"
    return {"verdict": "allow",
            "reason": (R_ALLOW_BUILTIN if builtin else R_ALLOW_GRANT),
            "ceiling": grant.standing_ceiling,
            "matched_grant_id": grant.id,
            "gate_version": WRITE_GATE_VERSION}
