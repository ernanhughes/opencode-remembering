# Bundled remembering engine (opencode-remembering product code).
"""Staged trust admission: admit | deny | quarantine with reason
codes. No scalar scores, no truth claims, no hidden oracle flags.
Stages fire in order; the trace names the stage that decided."""

from __future__ import annotations

from .instructions import screen_instruction
from .model import (
    R_ADMIT_AUTHORITATIVE,
    R_ADMIT_CONTEXT,
    R_ADMIT_CORROBORATED,
    R_DENY_INVARIANT,
    R_DENY_NON_GUIDING,
    R_DENY_PROVENANCE,
    R_DENY_RESTRICTED,
    R_DENY_SCOPE,
    R_DENY_TAINTED,
    R_DENY_UNCORROBORATED,
    R_DENY_UNTRUSTED,
    R_DENY_INSTRUCTION,
    R_DENY_REVOKED,
    R_QUARANTINE_CONFLICT,
    R_QUARANTINE_INSTRUCTION,
    R_QUARANTINE_NON_GUIDING,
    R_QUARANTINE_POISON,
    AdmissionRecord,
    TrustCandidate,
    Verdict,
)
from .policy import DIRECTING_ROLES, NON_GUIDING_ROLES, TrustPolicy
from .standing import (
    independent_roots,
    lineage_roots,
    revocation_tainted,
)

LEVELS = ("T0", "S1", "S2", "S3", "FULL")


def _at_least(level: str, minimum: str) -> bool:
    order = {name: i for i, name in enumerate(LEVELS)}
    return order[level] >= order[minimum]


class TrustContext:
    def __init__(self, policy: TrustPolicy,
                 revoked: set[str] | None = None,
                 restricted: dict[str, list[str]] | None = None,
                 derived_from: dict[str, list[str]] | None = None,
                 known_sources: set[str] | None = None,
                 project_id: str = "",
                 caller_scope: str = "default",
                 level: str = "FULL",
                 standing_override: dict[str, str] | None = None,
                 write_ceiling: dict[str, str] | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"TRUST_INVALID_LEVEL:{level!r}")
        self.policy = policy
        self.revoked = set(revoked or ())
        self.restricted = dict(restricted or {})
        self.derived_from = {k: list(v)
                             for k, v in (derived_from or {}).items()}
        self.known_sources = set(known_sources or ())
        self.project_id = project_id
        self.caller_scope = caller_scope
        self.level = level
        # Two-key standing (Stage 9): explicit-memory sources carry a
        # write-policy ceiling that caps the trust-resolved class.
        # Empty for all ordinary sources: their behavior is unchanged.
        self.standing_override = dict(standing_override or {})
        self.write_ceiling = dict(write_ceiling or {})
        self._roots: dict[str, frozenset[str]] = {}

    def class_of(self, source_id: str) -> tuple[str, str | None]:
        """Resolved (policy_class, write_ceiling|None). The caller
        combines them with the two-key minimum; the gate resolves the
        effective class through effective_of."""
        from .policy import match_rule

        rule, _ = match_rule(self.policy, source_id)
        policy_class = (rule.source_class if rule is not None
                        else self.policy.default_source_class)
        return policy_class, self.write_ceiling.get(source_id)

    def effective_of(self, source_id: str) -> tuple[str, str, str | None]:
        """(effective_class, policy_class, ceiling). Without a ceiling
        the effective class is the policy class (frozen behavior)."""
        policy_class, ceiling = self.class_of(source_id)
        if ceiling is None:
            return policy_class, policy_class, None
        from remembering.write.model import effective_class

        return (effective_class(policy_class, ceiling), policy_class,
                ceiling)

    def roots_of(self, source_id: str) -> frozenset[str]:
        if source_id not in self._roots:
            self._roots[source_id] = lineage_roots(
                source_id, self.derived_from)
        return self._roots[source_id]


def _record(candidate: TrustCandidate, verdict: Verdict, reason: str,
            stage: str, detail: str = "") -> AdmissionRecord:
    return AdmissionRecord(
        unit_id=candidate.unit_id, source_id=candidate.source_id,
        verdict=verdict, reason=reason, stage=stage, detail=detail)


def judge_all(candidates: list[TrustCandidate],
              ctx: TrustContext) -> list[AdmissionRecord]:
    """Two passes: base verdicts (no peer dependence), then
    refutation/corroboration/conflict resolution among peers."""
    base = [_base_verdict(c, ctx) for c in candidates]
    if ctx.level == "T0":
        return base
    by_id = {c.unit_id: c for c in candidates}
    base_by_id = {r.unit_id: r for r in base}
    out: list[AdmissionRecord] = []
    for candidate, record in zip(candidates, base):
        if record.verdict is Verdict.DENY or record.stage in (
                "scope", "pipeline_invariant", "revocation",
                "revocation_inheritance", "provenance"):
            out.append(record)
            continue
        resolved = _resolve_peer_dependent(candidate, record, by_id,
                                           base_by_id, ctx)
        out.append(resolved)
    return out


def _base_verdict(candidate: TrustCandidate,
                  ctx: TrustContext) -> AdmissionRecord:
    policy = ctx.policy
    if ctx.level == "T0":
        return _record(candidate, Verdict.ADMIT, R_ADMIT_CONTEXT,
                       "baseline", "T0: no standing gate")

    # Scope defense in depth (S2+; T0/S1 leave it to project isolation).
    if _at_least(ctx.level, "S2") and candidate.project_id != ctx.project_id:
        return _record(candidate, Verdict.DENY, R_DENY_SCOPE, "scope",
                       f"candidate project {candidate.project_id!r} != "
                       f"{ctx.project_id!r}")

    # Temporal pipeline invariant: superseded evidence must already be
    # gone from influence; if it arrives, fail safe, loudly.
    if candidate.temporal_status == "superseded":
        return _record(candidate, Verdict.DENY, R_DENY_INVARIANT,
                       "pipeline_invariant",
                       "superseded candidate reached trust admission")

    # Revocation (S1+).
    if candidate.source_id in ctx.revoked:
        return _record(candidate, Verdict.DENY, R_DENY_REVOKED,
                       "revocation",
                       f"source {candidate.source_id!r} revoked")
    tainted = revocation_tainted(candidate.source_id, ctx.revoked,
                                 ctx.derived_from)
    if tainted:
        return _record(candidate, Verdict.DENY, R_DENY_TAINTED,
                       "revocation_inheritance",
                       f"derives from revoked {tainted}")

    # Derived provenance must resolve (S1+).
    if candidate.role == "derived_restatement":
        missing = [ref for ref in candidate.derived_from
                   if ref not in ctx.known_sources]
        if not candidate.derived_from or missing:
            return _record(candidate, Verdict.DENY, R_DENY_PROVENANCE,
                           "provenance",
                           f"unresolvable derived_from={list(candidate.derived_from)}")

    # Standing classes and roles (S2+).
    if _at_least(ctx.level, "S2"):
        from .policy import match_rule

        rule, _ = match_rule(policy, candidate.source_id)
        effective, policy_class, ceiling = ctx.effective_of(
            candidate.source_id)
        source_class = effective
        role = rule.role if rule is not None else candidate.role
        ceiling_note = (f" trust_policy_class={policy_class} "
                        f"write_ceiling={ceiling} "
                        f"effective={effective}"
                        if ceiling is not None else "")
        if source_class == "untrusted":
            return _record(candidate, Verdict.DENY, R_DENY_UNTRUSTED,
                           "standing",
                           f"source class untrusted"
                           f"{f' (rule {rule.id})' if rule else ''}"
                           f"{ceiling_note}")
        if role in NON_GUIDING_ROLES:
            return _record(candidate, Verdict.DENY, R_DENY_NON_GUIDING,
                           "kind_authority",
                           f"role {role!r} never guides action")
        if rule is not None and rule.restricted_to and _at_least(
                ctx.level, "FULL"):
            if ctx.caller_scope not in rule.restricted_to:
                return _record(candidate, Verdict.DENY, R_DENY_RESTRICTED,
                               "restriction",
                               f"restricted to {list(rule.restricted_to)}; "
                               f"caller {ctx.caller_scope!r}")

    # Naive directive screen (S1): imperative content from a
    # non-authoritative class cannot settle; quarantine it.
    screen = screen_instruction(candidate.text)
    if screen["directive_like"] and ctx.level == "S1":
        effective, _, _ = ctx.effective_of(candidate.source_id)
        cls = effective
        if cls != "authoritative":
            return _record(candidate, Verdict.QUARANTINE,
                           R_QUARANTINE_POISON, "instruction_naive",
                           f"matched {screen['matched']!r}")

    return _record(candidate, Verdict.ADMIT, R_ADMIT_CONTEXT,
                   "default", "no stage objected")


def _resolve_peer_dependent(candidate: TrustCandidate,
                            base: AdmissionRecord,
                            by_id: dict, base_by_id: dict,
                            ctx: TrustContext) -> AdmissionRecord:
    if base.verdict is not Verdict.ADMIT:
        return base
    policy = ctx.policy
    from .policy import match_rule

    rule, _ = match_rule(policy, candidate.source_id)
    effective, _, _ = ctx.effective_of(candidate.source_id)
    source_class = effective
    role = rule.role if rule is not None else candidate.role
    screen = screen_instruction(candidate.text)
    authoritative = source_class == "authoritative"

    # Refuters with standing: currently admitted authoritative peers
    # named in refuted_by (for instruction denial), plus any admitted
    # peer (for conflict quarantine: contest needs standing, not
    # authority).
    standing_refuters = [
        ref for ref in candidate.refuted_by
        if ref in ctx.known_sources and _refuter_admits(
            ref, by_id, base_by_id, ctx)]
    admitted_refuters = [
        ref for ref in candidate.refuted_by
        if ref in ctx.known_sources and _refuter_present(
            ref, by_id, base_by_id)]

    directing = (role in DIRECTING_ROLES) or screen["directive_like"]

    if screen["directive_like"] and _at_least(ctx.level, "FULL"):
        if standing_refuters:
            return _record(candidate, Verdict.DENY, R_DENY_INSTRUCTION,
                           "instruction_refuted",
                           f"refuted by {standing_refuters}")
        corroborators = _corroborators(candidate, by_id, base_by_id, ctx)
        if authoritative:
            return _record(candidate, Verdict.ADMIT,
                           R_ADMIT_AUTHORITATIVE, "authority",
                           "authoritative directive")
        if corroborators:
            return _record(candidate, Verdict.ADMIT, R_ADMIT_CORROBORATED,
                           "corroboration",
                           f"corroborated by {corroborators}")
        matched = screen["matched"]
        return _record(candidate, Verdict.QUARANTINE,
                       R_QUARANTINE_INSTRUCTION, "instruction",
                       "unverified directive"
                       + (f" matched {matched!r}" if matched else ""))

    # Corroborated conflict: both sides standing -> quarantine both.
    if _at_least(ctx.level, "FULL") and admitted_refuters:
        mine = _corroborators(candidate, by_id, base_by_id, ctx,
                              include_self=True)
        if mine:
            return _record(candidate, Verdict.QUARANTINE,
                           R_QUARANTINE_CONFLICT, "conflict",
                           f"conflicts with {admitted_refuters}; both "
                           "sides corroborated")

    # Corroboration requirement (S3+): non-authoritative directing
    # content needs independent corroboration.
    if directing and not authoritative and _at_least(ctx.level, "S3"):
        corroborators = _corroborators(candidate, by_id, base_by_id, ctx)
        if not corroborators:
            return _record(candidate, Verdict.DENY, R_DENY_UNCORROBORATED,
                           "corroboration",
                           "directing content without independent "
                           "corroboration")
        return _record(candidate, Verdict.ADMIT, R_ADMIT_CORROBORATED,
                       "corroboration",
                       f"corroborated by {corroborators}")

    if authoritative and _at_least(ctx.level, "S2"):
        return _record(candidate, Verdict.ADMIT, R_ADMIT_AUTHORITATIVE,
                       "authority", "authoritative source")
    return base


def _refuter_present(ref_source: str, by_id: dict,
                     base_by_id: dict) -> bool:
    for unit_id, candidate in by_id.items():
        if candidate.source_id != ref_source:
            continue
        record = base_by_id.get(unit_id)
        if record is not None and record.verdict is not Verdict.DENY:
            return True
    return False


def _refuter_admits(ref_source: str, by_id: dict,
                    base_by_id: dict, ctx: TrustContext) -> bool:
    for unit_id, candidate in by_id.items():
        if candidate.source_id != ref_source:
            continue
        record = base_by_id.get(unit_id)
        if record is None or record.verdict is Verdict.DENY:
            continue
        effective, _, _ = ctx.effective_of(candidate.source_id)
        if effective == "authoritative" and \
                candidate.source_id not in ctx.revoked:
            return True
    return False


def _corroborators(candidate: TrustCandidate, by_id: dict,
                   base_by_id: dict, ctx: TrustContext,
                   include_self: bool = False) -> list[str]:
    """Independent corroborators: same claim_key, disjoint root
    lineages, no DENY verdict, no standing refuters of their own."""
    if not candidate.claim_key:
        return []
    mine = ctx.roots_of(candidate.source_id)
    out: list[str] = []
    for unit_id, peer in by_id.items():
        if peer.unit_id == candidate.unit_id and not include_self:
            continue
        if peer.claim_key != candidate.claim_key:
            continue
        record = base_by_id.get(unit_id)
        if record is None or record.verdict is Verdict.DENY:
            continue
        if ctx.roots_of(peer.source_id) & mine:
            continue
        out.append(peer.source_id)
    if include_self:
        return sorted(set(out + [candidate.source_id]))
    return sorted(set(out))
