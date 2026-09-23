# Bundled remembering engine (opencode-remembering product code).
"""Safe frame-control policy and frame-conditioned candidate handling.

Control mapping (frozen):

    DECLARED / CORROBORATED -> HARD_FRAME
    INFERRED                -> SOFT_FRAME
    CONFLICTING / STALE / UNKNOWN -> QUERY_ONLY

HARD_FRAME may order by work-type preferences, enforce project scope,
and exclude non-preferred classes only when the ProjectFrame
explicitly opts in (hard_exclude) — every exclusion traced.
SOFT_FRAME may add (objective expansion) and reorder, but never
erases baseline evidence: every query-only candidate survives.
QUERY_ONLY is the Stage 3 path untouched.

No universal scalar score: retrieval, temporal, frame eligibility,
ordering, and budget stay separate stages with separate reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Establishment, EstablishmentResult, FrameControl

# -- reason codes (stable, asserted in tests) ---------------------------

F_DECLARED = "frame.declared"
F_CORROBORATED = "frame.independent_corroboration"
F_INFERRED = "frame.inferred_only"
F_CONFLICT = "frame.conflicting_signals"
F_STALE = "frame.stale_prior"
F_UNKNOWN = "frame.unknown"
F_HARD = "frame.hard"
F_SOFT = "frame.soft"
F_QUERY_ONLY = "frame.query_only"
F_OBJECTIVE_EXPANSION = "frame.objective_expansion"
F_PREFERRED_CLASS = "frame.preferred_class"
F_OUT_OF_SCOPE = "frame.out_of_project_scope"
F_SOFT_RETAINED = "frame.absent_class_retained_soft"
F_HARD_EXCLUDED = "frame.absent_class_excluded_hard"
F_NO_FRAME = "frame.no_project_frame"

ESTABLISHMENT_REASONS = {
    Establishment.DECLARED: F_DECLARED,
    Establishment.CORROBORATED: F_CORROBORATED,
    Establishment.INFERRED: F_INFERRED,
    Establishment.CONFLICTING: F_CONFLICT,
    Establishment.STALE: F_STALE,
    Establishment.UNKNOWN: F_UNKNOWN,
}


def control_for(establishment: Establishment) -> FrameControl:
    if establishment in (Establishment.DECLARED, Establishment.CORROBORATED):
        return FrameControl.HARD_FRAME
    if establishment is Establishment.INFERRED:
        return FrameControl.SOFT_FRAME
    return FrameControl.QUERY_ONLY


@dataclass
class FramedCandidate:
    chunk_id: str
    source_id: str
    evidence_class: str
    in_scope: bool = True
    preferred: bool = False
    eligible: bool = True
    excluded_reason: str | None = None
    frame_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "evidence_class": self.evidence_class,
            "in_scope": self.in_scope,
            "preferred": self.preferred,
            "eligible": self.eligible,
            "excluded_reason": self.excluded_reason,
            "frame_reasons": list(self.frame_reasons),
        }


def apply_scope(candidates: list[FramedCandidate],
                include_projects: tuple[str, ...]) -> None:
    """Project scope enforcement (HARD only, applied by the caller).
    Empty include_projects means the whole project is in scope."""
    if not include_projects:
        return
    for candidate in candidates:
        if not any(candidate.source_id.startswith(prefix)
                   for prefix in include_projects):
            candidate.in_scope = False
            candidate.eligible = False
            candidate.excluded_reason = F_OUT_OF_SCOPE
            candidate.frame_reasons.append(F_OUT_OF_SCOPE)


def order_and_select(candidates: list[FramedCandidate],
                     preferred_classes: tuple[str, ...],
                     control: FrameControl,
                     hard_exclude: bool) -> tuple[list, list]:
    """Order (preferred first, stable) and select per control mode.
    Returns (selected, excluded). SOFT never excludes; HARD excludes
    out-of-scope always and non-preferred classes only when the
    ProjectFrame explicitly opts in."""
    for candidate in candidates:
        candidate.preferred = (
            candidate.evidence_class in preferred_classes)
        if candidate.preferred:
            candidate.frame_reasons.append(F_PREFERRED_CLASS)
    ordered = sorted(candidates,
                     key=lambda c: (not c.preferred, c.chunk_id))
    if control is FrameControl.SOFT_FRAME:
        for candidate in ordered:
            if candidate.eligible and not candidate.preferred:
                candidate.frame_reasons.append(F_SOFT_RETAINED)
        return ([c for c in ordered if c.eligible],
                [c for c in ordered if not c.eligible])
    if control is FrameControl.HARD_FRAME:
        selected: list[FramedCandidate] = []
        excluded: list[FramedCandidate] = []
        for candidate in ordered:
            if not candidate.eligible:
                excluded.append(candidate)
                continue
            if not candidate.preferred and hard_exclude \
                    and preferred_classes:
                candidate.eligible = False
                candidate.excluded_reason = F_HARD_EXCLUDED
                candidate.frame_reasons.append(F_HARD_EXCLUDED)
                excluded.append(candidate)
            else:
                selected.append(candidate)
        return selected, excluded
    # QUERY_ONLY: framing observes, never selects.
    return list(candidates), []


def control_reason(control: FrameControl,
                   establishment: Establishment) -> str:
    if control is FrameControl.HARD_FRAME:
        return F_HARD
    if control is FrameControl.SOFT_FRAME:
        return F_SOFT
    return F_QUERY_ONLY
