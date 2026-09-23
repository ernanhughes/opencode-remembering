# Bundled remembering engine (opencode-remembering product code).
"""Decisive selection policy: staged, deterministic, reason-coded.

Stages: group -> disagreement -> decisive classes -> provenance
closure -> redundancy suppression -> ordering -> budget. Each stage
is visible in the trace. No LLM, no scalar importance score, no
truth claims, no consensus merging.
"""

from __future__ import annotations

from . import budget as _budget
from . import provenance as _provenance
from . import redundancy as _redundancy
from .model import (
    D_BUDGET,
    D_DUPLICATE_SPAN,
    D_LOW_VALUE,
    D_REDUNDANT_CLAIM,
    D_REDUNDANT_ECHO,
    R_DISAGREEMENT,
    R_PROVENANCE,
    S_CONSTRAINT,
    S_CURRENT_AUTHORITATIVE,
    S_CURRENT_STATE,
    S_DIRECT_ANSWER,
    S_DISAGREEMENT,
    S_NEGATIVE,
    S_PREFERRED,
    S_PROVENANCE,
    S_RECALL,
    S_SUPPORT,
    SELECTION_POLICY_VERSION,
    Disposition,
    SelectionCandidate,
    SelectionClass,
    SelectionRecord,
    SelectionResult,
)

_DIRECTING_ROLES = ("decision", "production_state", "directive")


def select(candidates: list[SelectionCandidate],
           derived_from: dict[str, list[str]] | None = None,
           preferred_sources: set[str] | None = None,
           max_chars: int = 4000,
           max_results: int = 6,
           mode: str = "decisive") -> SelectionResult:
    """Select the smallest provenance-bearing set. Modes: "decisive"
    (influence), "preserving" (recall: keep everything admitted,
    groups computed for trace only), "full" (admitted-full baseline:
    pre-selection ordering + budget only, no suppression)."""
    if mode not in ("decisive", "preserving", "full"):
        raise ValueError(f"SELECT_BAD_MODE:{mode!r}")
    derived_from = derived_from or {}

    def roots_of(source_id: str) -> frozenset[str]:
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

    groups = _redundancy.group_candidates(candidates, roots_of)
    records = {c.chunk_id: SelectionRecord(
        chunk_id=c.chunk_id, source_id=c.source_id,
        chars=len(c.text), original_chars=len(c.text))
        for c in candidates}
    by_id = {c.chunk_id: c for c in candidates}

    if mode == "preserving":
        for record in records.values():
            record.selection_class = SelectionClass.CONTEXTUAL
            record.disposition = Disposition.SELECT
            record.reason = S_RECALL
        ordered = sorted(records.values(),
                         key=lambda r: (by_id[r.chunk_id].fused_rank,
                                        r.chunk_id))
        for order, record in enumerate(ordered):
            record.selection_order = order
        return SelectionResult(
            selected=ordered, dropped=[],
            groups=[g.to_dict() for g in groups])

    if mode == "full":
        for candidate in candidates:
            record = records[candidate.chunk_id]
            record.selection_class, record.reason = _classify(candidate)
            record.disposition = Disposition.SELECT
        alive = list(records.values())
        required: set[str] = set()
        ranks = {c.chunk_id: c.fused_rank for c in candidates}
        sources = {c.chunk_id: c.source_id for c in candidates}
        budgeted = _budget.enforce(alive, max_chars, max_results,
                                   preferred_sources or set(), ranks,
                                   sources, required)
        for order, record in enumerate(budgeted.kept):
            record.selection_order = order
        return SelectionResult(
            selected=budgeted.kept, dropped=budgeted.dropped_budget,
            groups=[g.to_dict() for g in groups],
            budget_insufficient=budgeted.insufficient)

    # Dispute groups: shared dispute keys, both sides retained.
    dispute_members: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.dispute_key:
            dispute_members.setdefault(candidate.dispute_key, []).append(
                candidate.chunk_id)

    # Classify.
    for candidate in candidates:
        record = records[candidate.chunk_id]
        record.selection_class, record.reason = _classify(candidate)

    # Redundancy suppression: one representative per group; echoes
    # collapse to their representative unless a member is decisive
    # for independent reasons (dispute, constraint, negative).
    for group in groups:
        members = [by_id[cid] for cid in group.members]
        protected = [c.chunk_id for c in members
                     if records[c.chunk_id].selection_class
                     is SelectionClass.DECISIVE
                     or c.dispute_key or c.negative or c.constraint_hit]
        if group.kind in ("claim", "echo", "identical_content"):
            rep = _redundancy.choose_representative(
                group, members, _priority_of)
            group.representative = rep
            group.reason += f"; representative {rep}"
            for cid in group.members:
                if cid != rep and cid not in protected:
                    records[cid].selection_class = SelectionClass.REDUNDANT
                    records[cid].disposition = Disposition.DROP_REDUNDANT
                    records[cid].reason = (
                        D_REDUNDANT_ECHO if group.kind == "echo"
                        else D_REDUNDANT_CLAIM)
                    records[cid].group_id = group.group_id
        elif group.kind == "duplicate_span":
            rep = sorted(group.members)[0]
            group.representative = rep
            for cid in group.members:
                if cid != rep:
                    records[cid].selection_class = SelectionClass.REDUNDANT
                    records[cid].disposition = Disposition.DROP_REDUNDANT
                    records[cid].reason = D_DUPLICATE_SPAN
                    records[cid].group_id = group.group_id

    # Disagreement retention overrides redundancy.
    for key, members in dispute_members.items():
        if len(members) > 1:
            for cid in members:
                records[cid].selection_class = SelectionClass.DECISIVE
                records[cid].disposition = Disposition.RETAIN_DISAGREEMENT
                records[cid].reason = R_DISAGREEMENT
                _ = key

    # Provenance closure over the would-be-selected set.
    provisional = {cid for cid, r in records.items()
                   if r.disposition not in (Disposition.DROP_REDUNDANT,)}
    grounding = _provenance.closure(provisional, candidates, roots_of)
    for chunk_id, roots in grounding.items():
        for root_chunk in roots:
            record = records[root_chunk]
            if record.disposition is Disposition.DROP_REDUNDANT:
                record.disposition = Disposition.RETAIN_PROVENANCE
                record.reason = R_PROVENANCE
                if record.selection_class is SelectionClass.REDUNDANT:
                    record.selection_class = SelectionClass.SUPPORTING
            record.provenance_for.append(chunk_id)
            records[chunk_id].grounded_by.append(root_chunk)
    # Invariant: every selected derived claim whose roots exist in the
    # pool must be grounded. Missing roots outside the pool are an
    # uncovered note, never a silent repair.
    pool_ids = {c.chunk_id for c in candidates}
    for cid in provisional:
        candidate = by_id[cid]
        if not candidate.derived_from:
            continue
        roots = {c.chunk_id for c in candidates
                 if c.source_id in roots_of(candidate.source_id)}
        if roots and not (roots & provisional):
            raise ValueError(
                f"SELECT_PROVENANCE_INVARIANT:{cid}: grounding roots "
                f"{sorted(roots)} in pool but none selected")

    # Default dispositions for the undecided.
    for cid, record in records.items():
        if record.disposition is Disposition.SELECT:
            candidate = by_id[cid]
            if record.selection_class is SelectionClass.DECISIVE:
                record.disposition = Disposition.RETAIN_DECISIVE
                record.reason = _decisive_reason(candidate)
            elif record.selection_class is SelectionClass.REDUNDANT:
                record.disposition = Disposition.DROP_REDUNDANT
            else:
                record.disposition = Disposition.SELECT
                if record.reason == D_LOW_VALUE:
                    record.reason = _support_reason(candidate)

    alive = [r for r in records.values()
             if r.disposition is not Disposition.DROP_REDUNDANT]
    dropped_redundant = [r for r in records.values()
                         if r.disposition is Disposition.DROP_REDUNDANT]
    required = {r.chunk_id for r in alive
                if r.disposition in (Disposition.RETAIN_DISAGREEMENT,
                                     Disposition.RETAIN_DECISIVE,
                                     Disposition.RETAIN_PROVENANCE)}
    for r in alive:
        required.update(g for g in r.grounded_by)
    ranks = {c.chunk_id: c.fused_rank for c in candidates}
    sources = {c.chunk_id: c.source_id for c in candidates}
    budgeted = _budget.enforce(alive, max_chars, max_results,
                               preferred_sources or set(), ranks, sources,
                               required)
    for order, record in enumerate(budgeted.kept):
        record.selection_order = order
    dropped = dropped_redundant + budgeted.dropped_budget
    for order, record in enumerate(dropped):
        if record.selection_order < 0:
            record.selection_order = len(budgeted.kept) + order
    return SelectionResult(
        selected=budgeted.kept, dropped=dropped,
        groups=[g.to_dict() for g in groups],
        budget_insufficient=budgeted.insufficient)


def _priority_of(candidate: SelectionCandidate) -> tuple:
    authoritative = 0 if candidate.source_class == "authoritative" else 1
    decisive = (0 if candidate.role in _DIRECTING_ROLES
                or candidate.constraint_hit else 1)
    return (authoritative, decisive, len(candidate.text))


def _classify(candidate: SelectionCandidate) -> tuple:
    # Authority with no contrary temporal evidence decides: temporal
    # suppression already removed proven-superseded candidates, and
    # unmodelled evidence must not be demoted for lacking semantics.
    # The item trace still carries the true temporal status.
    if (candidate.source_class == "authoritative"
            and candidate.role in ("decision", "production_state")
            and candidate.temporal_status in ("current", "not_modelled")):
        return SelectionClass.DECISIVE, S_CURRENT_AUTHORITATIVE
    if candidate.role == "production_state" \
            and candidate.temporal_status == "current":
        return SelectionClass.DECISIVE, S_CURRENT_STATE
    if candidate.constraint_hit:
        return SelectionClass.DECISIVE, S_CONSTRAINT
    if candidate.negative:
        return SelectionClass.DECISIVE, S_NEGATIVE
    if candidate.claim_key and candidate.derived_from:
        return SelectionClass.SUPPORTING, S_SUPPORT
    if candidate.frame_preferred:
        return SelectionClass.SUPPORTING, S_PREFERRED
    if candidate.claim_key or candidate.derived_from:
        return SelectionClass.SUPPORTING, S_SUPPORT
    return SelectionClass.CONTEXTUAL, D_LOW_VALUE


def _decisive_reason(candidate: SelectionCandidate) -> str:
    if candidate.constraint_hit:
        return S_CONSTRAINT
    if candidate.negative:
        return S_NEGATIVE
    if candidate.source_class == "authoritative":
        return S_CURRENT_AUTHORITATIVE
    return S_DIRECT_ANSWER


def _support_reason(candidate: SelectionCandidate) -> str:
    if candidate.frame_preferred:
        return S_PREFERRED
    if candidate.claim_key and candidate.derived_from:
        return S_PROVENANCE
    return S_SUPPORT


def policy_version() -> str:
    return SELECTION_POLICY_VERSION
