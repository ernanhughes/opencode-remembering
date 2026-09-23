# Bundled remembering engine (opencode-remembering product code).
"""Stage 4 frame contract evaluation. Deterministic, countable, and
kept separate from the routing (21) and temporal (16) contracts."""

from __future__ import annotations

from .establish import establish
from .model import (
    Establishment,
    FrameControl,
    ProjectFrame,
    WorkSignal,
    WorkTypeSpec,
)
from .policy import (
    F_HARD_EXCLUDED,
    apply_scope,
    control_for,
    order_and_select,
)
from .signals import evidence_class


def _frame() -> ProjectFrame:
    return ProjectFrame(
        project_id="test-project",
        version="v1",
        purpose="test",
        work_types=(
            WorkTypeSpec(name="implementation",
                         match_terms=("implement", "migration", "build")),
            WorkTypeSpec(name="architecture_review",
                         match_terms=("architecture", "review", "design")),
            WorkTypeSpec(name="release_readiness",
                         match_terms=("release", "checklist", "blocker")),
        ),
        evidence_preferences=(
            ("implementation", ("source_code", "test_result", "decision")),
            ("architecture_review", ("architecture", "decision")),
            ("release_readiness", ("release_note", "test_result")),
        ),
    )


def _sig(sid: str, kind: str, text: str) -> WorkSignal:
    return WorkSignal(signal_id=sid, kind=kind, text=text,
                      observed_at="2026-09-24T00:00:00Z")


def evaluate_frame() -> dict:
    """Deterministic Stage 4 contract. Categories stay separate."""
    results: dict[str, dict] = {}

    def record(category: str, ok: bool, note: str = "") -> None:
        entry = results.setdefault(category, {"correct": 0, "total": 0,
                                              "failures": []})
        entry["total"] += 1
        if ok:
            entry["correct"] += 1
        else:
            entry["failures"].append(note)

    frame = _frame()

    # declared
    r = establish([_sig("u1", "user_message",
                        "Implement the PostgreSQL storage migration.")],
                  frame)
    record("declared",
           r.establishment is Establishment.DECLARED
           and r.work_type == "implementation"
           and control_for(r.establishment) is FrameControl.HARD_FRAME
           and r.supporting_refs == ("u1",)
           and "Implement the PostgreSQL" in r.objective,
           f"{r.establishment}/{r.work_type}")

    # corroborated
    r = establish([_sig("u1", "user_message", "Fix the migration."),
                   _sig("t1", "test_failure",
                        "migration integration test failing.")],
                  frame)
    record("corroborated",
           r.establishment is Establishment.CORROBORATED
           and control_for(r.establishment) is FrameControl.HARD_FRAME
           and set(r.supporting_refs) == {"u1", "t1"},
           f"{r.establishment}/{r.supporting_refs}")

    # inferred (weak single signal)
    r = establish([_sig("t1", "tool_result",
                        "migration validation output looks off.")],
                  frame)
    record("inferred",
           r.establishment is Establishment.INFERRED
           and control_for(r.establishment) is FrameControl.SOFT_FRAME,
           f"{r.establishment}")

    # conflicting
    r = establish([_sig("u1", "user_message", "Prepare the release notes."),
                   _sig("a1", "agent_task",
                        "Refactor the persistence architecture.")],
                  frame)
    record("conflicting",
           r.establishment is Establishment.CONFLICTING
           and control_for(r.establishment) is FrameControl.QUERY_ONLY
           and len(r.conflicting_refs) == 2,
           f"{r.establishment}")

    # stale
    r = establish([_sig("u2", "user_message", "Fix the failing migration.")],
                  frame, prior_work_type="architecture_review")
    record("stale",
           r.establishment is Establishment.STALE
           and r.prior_work_type == "architecture_review"
           and control_for(r.establishment) is FrameControl.QUERY_ONLY,
           f"{r.establishment}")

    # unknown
    r = establish([_sig("u1", "user_message", "Take a look at this.")],
                  frame)
    record("unknown",
           r.establishment is Establishment.UNKNOWN
           and control_for(r.establishment) is FrameControl.QUERY_ONLY,
           f"{r.establishment}")

    # soft retention: inferred frame keeps non-preferred baseline items
    from .policy import FramedCandidate

    cands = [FramedCandidate("c1", "a.md", "architecture"),
             FramedCandidate("c2", "b.md", "prose")]
    sel, exc = order_and_select(
        cands, ("architecture",), FrameControl.SOFT_FRAME, False)
    record("soft_retention",
           [c.chunk_id for c in sel] == ["c1", "c2"] and not exc
           and any("frame.absent_class_retained_soft" in c.frame_reasons
                   for c in sel),
           f"sel={[c.chunk_id for c in sel]}")

    # hard exclusion only with explicit opt-in
    strict = ProjectFrame(project_id="t", version="v9",
                          work_types=frame.work_types,
                          evidence_preferences=frame.evidence_preferences,
                          hard_exclude=True)
    _ = strict
    cands = [FramedCandidate("c1", "a.md", "architecture"),
             FramedCandidate("c2", "b.md", "prose")]
    sel, exc = order_and_select(
        cands, ("architecture",), FrameControl.HARD_FRAME, True)
    record("soft_retention",
           [c.chunk_id for c in sel] == ["c1"]
           and [c.chunk_id for c in exc] == ["c2"]
           and exc[0].excluded_reason == F_HARD_EXCLUDED,
           "hard opt-in exclusion broken")

    # explicit override
    r = establish([_sig("u1", "user_message", "Take a look at this.")],
                  frame,
                  explicit={"work_type": "release_readiness",
                            "objective": "Cut the release."})
    record("explicit_override",
           r.establishment is Establishment.DECLARED
           and r.source == "explicit"
           and r.work_type == "release_readiness",
           f"{r.establishment}/{r.source}")

    # evidence classification sanity
    record("explicit_override",
           evidence_class("src/store.py", "code") == "source_code"
           and evidence_class("src/store.test.ts", "code")
           == "test_result"
           and evidence_class("adr-007.md", "decision-record")
           == "decision",
           "evidence classes wrong")

    total = sum(v["total"] for v in results.values())
    correct = sum(v["correct"] for v in results.values())
    return {"categories": results, "checks_total": total,
            "checks_passed": correct, "passed": total == correct}
