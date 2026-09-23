# Bundled remembering engine (opencode-remembering product code).
"""Budget accounting: explicit, auditable, deterministic. Budget runs
after eligibility: unsafe, excluded, and redundant candidates never
consume it. When required evidence cannot fit, the trace says so
instead of silently dropping it."""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    BUDGET_POLICY_VERSION,
    Disposition,
    SelectionClass,
    SelectionRecord,
)

# Priority order: disagreements, decisive, provenance, supporting,
# contextual. Lower sorts first.
_CLASS_ORDER = {
    SelectionClass.DECISIVE: 0,
    SelectionClass.SUPPORTING: 1,
    SelectionClass.CONTEXTUAL: 2,
    SelectionClass.REDUNDANT: 3,
}

_DISPOSITION_ORDER = {
    Disposition.RETAIN_DISAGREEMENT: 0,
    Disposition.RETAIN_DECISIVE: 0,
    Disposition.RETAIN_PROVENANCE: 1,
    Disposition.SELECT: 2,
}


@dataclass
class BudgetResult:
    kept: list[SelectionRecord] = field(default_factory=list)
    dropped_budget: list[SelectionRecord] = field(default_factory=list)
    insufficient: bool = False
    chars_used: int = 0


def order_key(record: SelectionRecord, preferred: set[str],
              ranks: dict, sources: dict) -> tuple:
    preferred_flag = 0
    source = sources.get(record.chunk_id, "")
    if source in preferred:
        preferred_flag = -1
    return (
        _DISPOSITION_ORDER.get(record.disposition, 2),
        _CLASS_ORDER.get(record.selection_class, 2),
        preferred_flag,
        ranks.get(record.chunk_id, 10**9),
        source,
        record.chunk_id,
    )


def enforce(records: list[SelectionRecord], max_chars: int,
            max_results: int,
            preferred: set[str] | None = None,
            ranks: dict | None = None,
            sources: dict | None = None,
            required_ids: set[str] | None = None) -> BudgetResult:
    """Apply the budget over ordered records. Required ids (decisive,
    disagreement, provenance) that cannot fit set insufficient."""
    ordered = sorted(
        records,
        key=lambda r: order_key(r, preferred or set(), ranks or {},
                                sources or {}))
    kept: list[SelectionRecord] = []
    dropped: list[SelectionRecord] = []
    chars = 0
    count = 0
    for record in ordered:
        if count + 1 > max_results or chars + record.chars > max_chars:
            record.disposition = Disposition.DROP_BUDGET
            record.reason = "drop.budget"
            dropped.append(record)
            continue
        chars += record.chars
        count += 1
        kept.append(record)
    required_ids = required_ids or set()
    dropped_required = [r.chunk_id for r in dropped
                        if r.chunk_id in required_ids]
    return BudgetResult(kept=kept, dropped_budget=dropped,
                        insufficient=bool(dropped_required),
                        chars_used=chars)


def policy_version() -> str:
    return BUDGET_POLICY_VERSION
