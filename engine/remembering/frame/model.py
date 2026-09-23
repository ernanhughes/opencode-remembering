# Bundled remembering engine (opencode-remembering product code).
"""Frame domain model: versioned project config, observed signals,
ephemeral work state, establishment and control vocabularies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

PROJECT_FRAME_SCHEMA = "project-frame-v0.1"
WORK_FRAME_SCHEMA = "work-frame-v0.1"
ESTABLISHMENT_POLICY_VERSION = "frame-establishment-v0.1"
FRAME_CONTROL_POLICY_VERSION = "frame-control-v0.1"
FRAMING_ENGINE_VERSION = "framing-engine-v0.1"


class Establishment(str, Enum):
    DECLARED = "declared"
    CORROBORATED = "corroborated"
    INFERRED = "inferred"
    CONFLICTING = "conflicting"
    STALE = "stale"
    UNKNOWN = "unknown"


class FrameControl(str, Enum):
    HARD_FRAME = "hard"
    SOFT_FRAME = "soft"
    QUERY_ONLY = "query_only"


# Evidence classes compatible with baseline ingestion metadata.
# Small on purpose: a preference vocabulary, not a document ontology.
EVIDENCE_CLASSES = (
    "source_code",
    "test_result",
    "decision",
    "architecture",
    "release_note",
    "open_issue",
    "historical",
    "config",
    "prose",
)

# Signal kinds the engine understands. Hosts need not supply all.
SIGNAL_KINDS = (
    "user_message",
    "agent_task",
    "tool_result",
    "test_failure",
    "project_state",
    "event",
    "scheduled_job",
)

# Signal kinds decisive enough to declare a frame on their own.
DECISIVE_KINDS = ("user_message", "agent_task")


@dataclass(frozen=True)
class WorkTypeSpec:
    name: str
    match_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectFrame:
    project_id: str
    version: str
    purpose: str = ""
    objectives: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    include_projects: tuple[str, ...] = ()
    work_types: tuple[WorkTypeSpec, ...] = ()
    evidence_preferences: tuple[tuple[str, tuple[str, ...]], ...] = ()
    hard_exclude: bool = False
    schema_version: str = PROJECT_FRAME_SCHEMA

    def preferences_for(self, work_type: str) -> tuple[str, ...]:
        for name, classes in self.evidence_preferences:
            if name == work_type:
                return classes
        return ()

    def work_type_names(self) -> tuple[str, ...]:
        return tuple(w.name for w in self.work_types)


@dataclass(frozen=True)
class WorkSignal:
    signal_id: str
    kind: str
    text: str
    observed_at: str
    # Observation only: no confidence, no interpretation.

    def matched_types(self,
                      work_types: tuple[WorkTypeSpec, ...]) -> tuple[str, ...]:
        text = (self.text or "").lower()
        return tuple(w.name for w in work_types
                     if any(t.lower() in text for t in w.match_terms))


@dataclass(frozen=True)
class WorkFrame:
    work_frame_id: str
    project_id: str
    objective: str
    work_type: str
    as_of: str
    active_constraints: tuple[str, ...] = ()
    signals: tuple[WorkSignal, ...] = ()
    # Field provenance: which signal IDs license each primary field.
    provenance: tuple[tuple[str, tuple[str, ...]], ...] = ()
    establishment: Establishment = Establishment.UNKNOWN
    schema_version: str = WORK_FRAME_SCHEMA

    def provenance_for(self, field_name: str) -> tuple[str, ...]:
        for name, refs in self.provenance:
            if name == field_name:
                return refs
        return ()


@dataclass(frozen=True)
class EstablishmentResult:
    establishment: Establishment
    work_type: str | None
    objective: str
    supporting_refs: tuple[str, ...] = ()
    conflicting_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    source: str = "deterministic"  # or "explicit"
    prior_work_type: str | None = None

    def to_dict(self) -> dict:
        return {
            "establishment": self.establishment.value,
            "work_type": self.work_type,
            "objective": self.objective,
            "supporting_refs": list(self.supporting_refs),
            "conflicting_refs": list(self.conflicting_refs),
            "reasons": list(self.reasons),
            "source": self.source,
            "prior_work_type": self.prior_work_type,
        }
