# Bundled remembering engine (opencode-remembering product code).
"""Deterministic evidence-backed WorkFrame establishment. No LLM, no
scalar confidence: every selected field cites the WorkSignal IDs that
license it, and weakness is a first-class outcome (INFERRED,
CONFLICTING, STALE, UNKNOWN), never a hidden default.
"""

from __future__ import annotations

import hashlib

from .model import (
    DECISIVE_KINDS,
    Establishment,
    EstablishmentResult,
    ProjectFrame,
    WorkSignal,
)


def _signal_kinds(signals: list[WorkSignal]) -> set[str]:
    return {s.kind for s in signals}


def establish(signals: list[WorkSignal],
              project_frame: ProjectFrame | None,
              explicit: dict | None = None,
              prior_work_type: str | None = None,
              objective_fallback: str = "") -> EstablishmentResult:
    """Establish the current WorkFrame deterministically.

    explicit: {"work_type": ..., "objective": ...} from a valid caller
    declaration — stronger than any inference.
    prior_work_type: previously established type, for STALE detection.
    """
    if explicit:
        return _establish_explicit(explicit, signals, project_frame)
    if project_frame is None or not project_frame.work_types:
        return EstablishmentResult(
            Establishment.UNKNOWN, None, objective_fallback, (),
            (), ("frame.no_project_frame",))
    if not signals:
        return EstablishmentResult(
            Establishment.UNKNOWN, None, objective_fallback, (),
            (), ("frame.no_signals",))

    work_types = project_frame.work_types
    matches: dict[str, list[WorkSignal]] = {}
    for signal in signals:
        for name in signal.matched_types(work_types):
            matches.setdefault(name, []).append(signal)

    decisive: dict[str, list[WorkSignal]] = {}
    for name, sigs in matches.items():
        decisive_sigs = [s for s in sigs if s.kind in DECISIVE_KINDS]
        if decisive_sigs:
            decisive[name] = decisive_sigs

    def objective_from(sigs: list[WorkSignal]) -> str:
        # Objective is observable text, never invented: the first
        # decisive signal's own words, truncated.
        for signal in sigs:
            if signal.kind in DECISIVE_KINDS and signal.text.strip():
                return signal.text.strip()[:240]
        for signal in sigs:
            if signal.text.strip():
                return signal.text.strip()[:240]
        return objective_fallback

    if len(decisive) > 1:
        refs = sorted({s.signal_id for sigs in decisive.values()
                       for s in sigs})
        return EstablishmentResult(
            Establishment.CONFLICTING, None, objective_fallback, (),
            tuple(refs), ("frame.conflicting_signals",))
    if len(decisive) == 1:
        name = next(iter(decisive))
        supporting = matches[name]
        kinds = _signal_kinds(supporting)
        refs = tuple(s.signal_id for s in supporting)
        objective = objective_from(supporting)
        if prior_work_type is not None and prior_work_type != name:
            # A newer direct signal contradicts the prior frame. Without
            # independent corroboration for the new type, trust neither:
            # the shift is visible, control falls back.
            if len(kinds) < 2:
                return EstablishmentResult(
                    Establishment.STALE, name, objective, refs,
                    (), ("frame.stale_prior",),
                    prior_work_type=prior_work_type)
        if len(kinds) >= 2:
            return EstablishmentResult(
                Establishment.CORROBORATED, name, objective,
                refs, (), ("frame.independent_agreement",))
        return EstablishmentResult(
            Establishment.DECLARED, name, objective, refs,
            (), ("frame.declared",))
    if len(matches) == 1:
        name = next(iter(matches))
        refs = tuple(s.signal_id for s in matches[name])
        return EstablishmentResult(
            Establishment.INFERRED, name, objective_from(matches[name]),
            refs, (), ("frame.inferred_only",))
    if len(matches) > 1:
        refs = sorted({s.signal_id for sigs in matches.values()
                       for s in sigs})
        return EstablishmentResult(
            Establishment.CONFLICTING, None, objective_fallback, (),
            tuple(refs), ("frame.conflicting_signals",))
    return EstablishmentResult(
        Establishment.UNKNOWN, None, objective_fallback, (),
        (), ("frame.no_matching_signals",))


def _establish_explicit(explicit: dict, signals: list[WorkSignal],
                        project_frame: ProjectFrame | None) -> EstablishmentResult:
    work_type = explicit.get("work_type")
    objective = explicit.get("objective", "") or ""
    if not isinstance(work_type, str) or not work_type.strip():
        raise ValueError("FRAME_INVALID:explicit work needs work_type")
    work_type = work_type.strip()
    if project_frame is not None and project_frame.work_types:
        known = project_frame.work_type_names()
        if work_type not in known:
            raise ValueError(
                f"FRAME_INVALID:unknown work_type {work_type!r}; "
                f"declared types: {list(known)}")
    refs = tuple(s.signal_id for s in signals)
    return EstablishmentResult(
        Establishment.DECLARED, work_type, objective
        if isinstance(objective, str) else "", refs, (),
        ("frame.explicit_declaration",), source="explicit")


def work_frame_id(project_id: str, work_type: str | None,
                  refs: tuple[str, ...], as_of: str) -> str:
    material = "|".join([project_id, work_type or "", ",".join(sorted(refs)),
                         as_of])
    return "wf-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
