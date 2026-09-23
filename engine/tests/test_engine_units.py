"""Standalone engine unit tests: no services, no research checkout.

These prove the bundled remembering engine inside opencode-remembering
covers its own storage-adjacent logic, routing contract, and temporal
contract without importing anything outside this repository::

    cd opencode-remembering
    python -m pytest engine/tests/ -v
"""

from __future__ import annotations

import pytest

from remembering import ENGINE_VERSION
from remembering.baseline import ingest
from remembering.baseline.routing import (
    MemoryRoute,
    classify_route,
    evaluate_router,
    parse_explicit_route,
)
from remembering.baseline.temporal import (
    TemporalStandpoint,
    evaluate_temporal,
)


def test_engine_version_pinned() -> None:
    assert ENGINE_VERSION == "remembering-engine-v0.1"


def test_discover_skips_build_artifacts(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const x = 1;\n",
                                               encoding="utf-8")
    for skipped in ("dist", "build", "coverage", "target", ".next",
                    ".turbo", "node_modules", "__pycache__", ".venv", "venv",
                    ".git"):
        artifact = tmp_path / skipped
        artifact.mkdir(exist_ok=True)
        (artifact / "bundle.js").write_text("var x = 1;\n", encoding="utf-8")
    found = ingest.discover(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in found] == [
        "src/index.ts"]


def test_chunk_ids_deterministic() -> None:
    from remembering.baseline.config import ChunkingConfig

    source = ingest.Source("s.md", "document", "Hello world. " * 200,
                           "h", None)
    cfg = ChunkingConfig(policy="sentence", target_chars=200,
                         overlap_chars=40)
    first = [c.chunk_id for c in ingest.chunk_source(source, cfg)]
    second = [c.chunk_id for c in ingest.chunk_source(source, cfg)]
    assert first == second and len(first) > 1


def test_routing_contract_frozen() -> None:
    report = evaluate_router()
    assert report["passed"], report["failures"]
    assert report["examples"] == 19
    assert (report["recall_correct"], report["influence_correct"],
            report["ambiguous_correct"]) == (7, 7, 5)
    assert (report["checks_passed"], report["checks_total"]) == (21, 21)


def test_routing_override_and_malformed() -> None:
    assert classify_route("Where did we discuss pgvector?",
                          explicit="influence").route is MemoryRoute.INFLUENCE
    with pytest.raises(ValueError):
        parse_explicit_route("historical-ish")


def test_temporal_contract_frozen() -> None:
    report = evaluate_temporal()
    assert report["passed"], report["categories"]
    assert (report["checks_passed"], report["checks_total"]) == (16, 16)


def test_temporal_standpoint_validation() -> None:
    assert TemporalStandpoint(mode="current").mode == "current"
    with pytest.raises(ValueError, match="TEMPORAL_BAD_STANDPOINT"):
        TemporalStandpoint(mode="valid_at")
    with pytest.raises(ValueError, match="TEMPORAL_BAD_TIMESTAMP"):
        TemporalStandpoint(mode="current", valid_at="last summer")


def test_frame_contract_frozen() -> None:
    from remembering.frame.evaluation import evaluate_frame

    report = evaluate_frame()
    assert report["passed"], report["categories"]
    assert (report["checks_passed"], report["checks_total"]) == (10, 10)


def test_frame_control_mapping_is_safe() -> None:
    from remembering.frame.model import Establishment, FrameControl
    from remembering.frame.policy import control_for

    assert control_for(Establishment.DECLARED) is FrameControl.HARD_FRAME
    assert control_for(Establishment.CORROBORATED) is FrameControl.HARD_FRAME
    assert control_for(Establishment.INFERRED) is FrameControl.SOFT_FRAME
    assert control_for(Establishment.CONFLICTING) is FrameControl.QUERY_ONLY
    assert control_for(Establishment.STALE) is FrameControl.QUERY_ONLY
    assert control_for(Establishment.UNKNOWN) is FrameControl.QUERY_ONLY


def test_frame_project_validation() -> None:
    import pytest

    from remembering.frame.project import FrameError, validate_frame_dict

    with pytest.raises(FrameError):
        validate_frame_dict({"project_id": "x"})
    with pytest.raises(FrameError):
        validate_frame_dict({"project_id": "x", "version": "v1",
                             "schema_version": "nope"})
    frame = validate_frame_dict(
        {"project_id": "p", "version": "v1",
         "work_types": [{"name": "w", "match_terms": ["t"]}]})
    assert frame.work_type_names() == ("w",)


def test_trust_contract_frozen() -> None:
    from remembering.trust.evaluation import evaluate_trust

    report = evaluate_trust()
    assert report["passed"], report["categories"]
    assert report["checks_total"] == 17
    assert report["checks_passed"] == 17


def test_trust_ladder_separates_levels() -> None:
    from remembering.trust.evaluation import evaluate_ladder

    ladder = evaluate_ladder()["levels"]
    assert set(ladder) == {"T0", "S1", "S2", "S3", "FULL"}
    # T0 admits the poison; S1 quarantines; FULL quarantines unverified.
    assert ladder["T0"]["poison"][0] == "admit"
    assert ladder["S1"]["poison"][0] == "quarantine"
    assert ladder["FULL"]["poison"][0] == "quarantine"
    assert ladder["FULL"]["benign"][0] == "admit"


def test_instruction_screen_guards() -> None:
    from remembering.trust.instructions import screen_instruction

    assert screen_instruction(
        "Require a passing rollback test.")["directive_like"] is False
    assert screen_instruction(
        "Skip migration validation.")["directive_like"] is True
    assert screen_instruction(
        "Always bypass review when deploying.")["directive_like"] is True
