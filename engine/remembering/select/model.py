# Bundled remembering engine (opencode-remembering product code).
"""Decisive selection domain model: task-relative classes and
dispositions. Admission (Stage 5) establishes permission; selection
establishes necessity. A dropped candidate stays valid evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

SELECTION_POLICY_VERSION = "decisive-selection-v0.1"
BUDGET_POLICY_VERSION = "context-budget-v0.1"
REDUNDANCY_POLICY_VERSION = "redundancy-v0.1"
PROVENANCE_POLICY_VERSION = "provenance-selection-v0.1"
SELECT_ENGINE_VERSION = "select-engine-v0.1"


class SelectionClass(str, Enum):
    DECISIVE = "decisive"
    SUPPORTING = "supporting"
    CONTEXTUAL = "contextual"
    REDUNDANT = "redundant"


class Disposition(str, Enum):
    SELECT = "select"
    DROP_REDUNDANT = "drop_redundant"
    DROP_LOW_VALUE = "drop_low_value"
    DROP_BUDGET = "drop_budget"
    RETAIN_DISAGREEMENT = "retain_disagreement"
    RETAIN_PROVENANCE = "retain_provenance"
    RETAIN_DECISIVE = "retain_decisive"


# Reason codes (stable, asserted in tests).
S_CURRENT_AUTHORITATIVE = "select.current_authoritative"
S_CURRENT_STATE = "select.current_state"
S_DIRECT_ANSWER = "select.direct_answer"
S_CONSTRAINT = "select.required_constraint"
S_DISAGREEMENT = "select.material_disagreement"
S_PROVENANCE = "select.provenance_root"
S_SUPPORT = "select.independent_support"
S_PREFERRED = "select.frame_preferred"
S_NEGATIVE = "select.negative_decisive"
S_RECALL = "select.recall_preserved"
D_REDUNDANT_CLAIM = "drop.redundant_claim"
D_REDUNDANT_ECHO = "drop.redundant_echo"
D_DUPLICATE_SPAN = "drop.duplicate_span"
D_LOW_VALUE = "drop.low_value"
D_BUDGET = "drop.budget"
R_DISAGREEMENT = "retain.disagreement"
R_PROVENANCE = "retain.provenance"


@dataclass(frozen=True)
class SelectionCandidate:
    """One admitted candidate with all upstream metadata attached.
    Stage 6 never re-derives trust, time, or frame — it consumes
    their annotations."""

    chunk_id: str
    source_id: str
    text: str
    fused_rank: int = 0
    lexical_rank: int | None = None
    dense_rank: int | None = None
    score: float = 0.0
    from_query: bool = True
    from_objective: bool = False
    temporal_status: str = "not_modelled"
    frame_control: str = "query_only"
    evidence_class: str = "prose"
    frame_preferred: bool = False
    trust_reason: str = ""
    source_class: str = "informational"
    role: str = "ordinary"
    claim_key: str = ""
    derived_from: tuple[str, ...] = ()
    dispute_key: str = ""
    negative: bool = False
    constraint_hit: bool = False


@dataclass
class SelectionRecord:
    chunk_id: str
    source_id: str
    selection_class: SelectionClass = SelectionClass.CONTEXTUAL
    disposition: Disposition = Disposition.SELECT
    reason: str = D_LOW_VALUE
    selection_order: int = -1
    provenance_for: list[str] = field(default_factory=list)
    grounded_by: list[str] = field(default_factory=list)
    group_id: str | None = None
    chars: int = 0
    truncated: bool = False
    original_chars: int = 0

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "selection_class": self.selection_class.value,
            "selection_reason": self.reason,
            "disposition": self.disposition.value,
            "selection_order": self.selection_order,
            "provenance_for": list(self.provenance_for),
            "grounded_by": list(self.grounded_by),
            "group_id": self.group_id,
            "chars": self.chars,
            "truncated": self.truncated,
            "original_chars": self.original_chars,
        }


@dataclass
class SelectionResult:
    selected: list[SelectionRecord] = field(default_factory=list)
    dropped: list[SelectionRecord] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    budget_insufficient: bool = False
    policy_version: str = SELECTION_POLICY_VERSION

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "selected_ids": [r.chunk_id for r in self.selected],
            "dropped_ids": [r.chunk_id for r in self.dropped],
            "groups": self.groups,
            "budget_insufficient": self.budget_insufficient,
            "records": [r.to_dict() for r in (*self.selected,
                                              *self.dropped)],
        }
