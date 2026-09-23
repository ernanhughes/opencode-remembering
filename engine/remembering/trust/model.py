# Bundled remembering engine (opencode-remembering product code).
"""Trust/standing domain model: verdicts, candidate envelopes, standing
events, admission records. Trust governs whether remembered material
may influence behaviour; it never declares content true or false."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

TRUST_POLICY_SCHEMA = "trust-policy-v0.1"
TRUST_POLICY_VERSION = "trust-policy-v0.1"
STANDING_EVENT_SCHEMA = "standing-event-v0.1"
STANDING_RESOLVER_VERSION = "standing-resolver-v0.1"
INSTRUCTION_SCREEN_VERSION = "instruction-screen-v0.1"
TRUST_EVAL_VERSION = "trust-eval-v0.1"
TRUST_ENGINE_VERSION = "trust-engine-v0.1"


class Verdict(str, Enum):
    ADMIT = "admit"
    DENY = "deny"
    QUARANTINE = "quarantine"


# Reason codes (stable, asserted in tests).
R_ADMIT_AUTHORITATIVE = "admit.current_authoritative"
R_ADMIT_CORROBORATED = "admit.corroborated"
R_ADMIT_CONTEXT = "admit.relevant_context"
R_DENY_REVOKED = "deny.revoked_source"
R_DENY_TAINTED = "deny.revocation_tainted"
R_DENY_PROVENANCE = "deny.unresolvable_provenance"
R_DENY_SCOPE = "deny.cross_scope"
R_DENY_UNTRUSTED = "deny.untrusted_source"
R_DENY_NON_GUIDING = "deny.non_guiding"
R_DENY_RESTRICTED = "deny.private_scope"
R_DENY_INSTRUCTION = "deny.memory_instruction"
R_DENY_UNCORROBORATED = "deny.uncorroborated"
R_DENY_INVARIANT = "deny.pipeline_invariant"
R_QUARANTINE_POISON = "quarantine.suspected_poison"
R_QUARANTINE_CONFLICT = "quarantine.conflicting_evidence"
R_QUARANTINE_INSTRUCTION = "quarantine.unverified_instruction"
R_QUARANTINE_NON_GUIDING = "quarantine.non_guiding_role"


@dataclass(frozen=True)
class TrustCandidate:
    """One candidate with observable features only. No truth flags,
    no scalar scores, no hidden oracle labels."""

    unit_id: str
    source_id: str
    project_id: str
    text: str
    source_class: str = "informational"
    role: str = "ordinary"
    temporal_status: str = "not_modelled"
    frame_control: str = "query_only"
    claim_key: str = ""
    derived_from: tuple[str, ...] = ()
    refuted_by: tuple[str, ...] = ()
    restricted_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionRecord:
    unit_id: str
    source_id: str
    verdict: Verdict
    reason: str
    stage: str
    policy_version: str = TRUST_POLICY_VERSION
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "source_id": self.source_id,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "stage": self.stage,
            "policy_version": self.policy_version,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class StandingEvent:
    event_id: str
    subject_type: str  # "source" for Stage 5
    subject_id: str
    event_type: str  # STANDING_REVOKED | STANDING_RESTORED |
    #                # RESTRICTION_SET | RESTRICTION_CLEARED
    event_time: str
    recorded_at: str
    effective_from: str = ""
    scope: str = ""
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "event_type": self.event_type,
            "event_time": self.event_time,
            "recorded_at": self.recorded_at,
            "effective_from": self.effective_from,
            "scope": self.scope,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "StandingEvent":
        return cls(
            event_id=raw["event_id"],
            subject_type=raw.get("subject_type", "source"),
            subject_id=raw["subject_id"],
            event_type=raw["event_type"],
            event_time=raw["event_time"],
            recorded_at=raw["recorded_at"],
            effective_from=raw.get("effective_from", "") or "",
            scope=raw.get("scope", "") or "",
            evidence_refs=tuple(raw.get("evidence_refs", ())),
        )


STANDING_EVENT_TYPES = frozenset({
    "STANDING_REVOKED",
    "STANDING_RESTORED",
    "RESTRICTION_SET",
    "RESTRICTION_CLEARED",
})
