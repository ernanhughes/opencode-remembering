# Bundled remembering engine (opencode-remembering product code).
"""Stage 7 trace contract: deterministic service-free checks.
PostgreSQL-backed cases (persist, lookup, tamper, replay over live
traces) live in the bridge suite; this module pins the math."""

from __future__ import annotations

from .canonical import (
    bundle_digest_for,
    canonical_json,
    content_hash,
    input_digest_for,
    pool_digest_for,
    trace_id_for,
)
from .lifecycle import build_lifecycles, funnel
from .replay import (
    counterfactual_id,
    diff_traces,
    integrity_replay,
    reconstruct_bundle,
)


def _synthetic_lifecycles():
    pool = [
        {"chunk_id": "c1", "source_id": "a.md", "text": "alpha",
         "lexical_rank": 1, "dense_rank": 2, "score": 0.9},
        {"chunk_id": "c2", "source_id": "b.md", "text": "beta",
         "lexical_rank": 2, "dense_rank": 1, "score": 0.8},
    ]
    from .model import CandidateLifecycle  # noqa: F401
    import dataclasses

    temporal_selected = {"c1", "c2"}
    annotations = {"c1": _ann("current"), "c2": _ann("current")}
    from remembering.trust.model import AdmissionRecord, Verdict
    trust = {
        "c1": AdmissionRecord("c1", "a.md", Verdict.ADMIT,
                              "admit.relevant_context", "default"),
        "c2": AdmissionRecord("c2", "b.md", Verdict.DENY,
                              "deny.revoked_source", "revocation"),
    }
    from remembering.select.model import (
        Disposition,
        SelectionClass,
        SelectionRecord,
    )
    selection = {
        "c1": SelectionRecord("c1", "a.md", SelectionClass.DECISIVE,
                              Disposition.RETAIN_DECISIVE,
                              "select.current_authoritative", 0),
    }
    retrieval_index = {
        "c1": {"lexical_rank": 1, "dense_rank": 2, "fused_rank": 1,
               "paths": ["lexical", "dense"], "score": 0.9},
        "c2": {"lexical_rank": 2, "dense_rank": 1, "fused_rank": 2,
               "paths": ["lexical", "dense"], "score": 0.8},
    }
    return build_lifecycles(
        pool, temporal_selected, annotations, {"c1", "c2"}, True,
        trust, True, selection, ["c1"], retrieval_index)


class _Ann:
    def __init__(self, status):
        self.status = status


def _ann(status):
    return _Ann(status)


def _stored():
    lifecycles = _synthetic_lifecycles()
    bundle_content = "evidence c1"
    pool_entries = [{"chunk_id": c["candidate_id"],
                     "content_hash": c["content_hash"]} for c in lifecycles]
    semantic = {
        "schema_version": "context-trace-v0.1",
        "trace_id": "",
        "project": {"project_id": "p", "project_digest": "d"},
        "created_at": "2026-09-24T00:00:00Z",
        "request": {"query": "q", "route": "influence"},
        "route": {"route": "influence"},
        "versions": {"engine": "remembering-engine-v0.1"},
        "stages": {},
        "candidates": lifecycles,
        "candidate_pool": {"entries": pool_entries,
                           "candidate_pool_digest": pool_digest_for(
                               pool_entries)},
        "bundle": {"content": bundle_content, "chars": len(bundle_content),
                   "render_order": ["c1"],
                   "bundle_digest": bundle_digest_for(bundle_content)},
        "funnel": funnel(lifecycles),
        "timings": {"total_ms": 1.0},
    }
    from .canonical import identity_payload

    semantic["trace_id"] = trace_id_for(identity_payload(semantic))
    return semantic


def evaluate_trace() -> dict:
    results: dict[str, dict] = {}

    def record(category: str, ok: bool, note: str = "") -> None:
        entry = results.setdefault(category, {"correct": 0, "total": 0,
                                              "failures": []})
        entry["total"] += 1
        if ok:
            entry["correct"] += 1
        else:
            entry["failures"].append(note)

    # Content addressing: same payload same ID, changed payload differs.
    stored = _stored()
    from .canonical import identity_payload

    payload = identity_payload(stored)
    record("content_addressing",
           trace_id_for(payload) == stored["trace_id"]
           and stored["trace_id"].startswith("ctx_"),
           stored["trace_id"])
    altered = dict(payload)
    altered["request"] = {"query": "other", "route": "influence"}
    record("content_addressing",
           trace_id_for(altered) != stored["trace_id"],
           "mutation invisible")

    # Canonical form is stable and finite.
    record("canonical",
           canonical_json({"b": 1, "a": [1, 2, None]})
           == '{"a":[1,2,null],"b":1}',
           canonical_json({"b": 1}))
    record("digests",
           len(content_hash("x")) == 16
           and len(input_digest_for({"q": 1})) == 16,
           "digest shape")

    # Lifecycle terminals and funnel reconcile.
    lifecycles = _synthetic_lifecycles()
    by_id = {c["candidate_id"]: c for c in lifecycles}
    record("terminals",
           by_id["c1"]["terminal_stage"] == "FINAL_SELECTED"
           and by_id["c2"]["terminal_stage"] == "TRUST_DENIED",
           f"{[(c['candidate_id'], c['terminal_stage']) for c in lifecycles]}")
    fun = funnel(lifecycles)
    record("funnel",
           fun == {"retrieved": 2, "temporal_survivors": 2,
                   "frame_survivors": 2, "admitted": 1, "selected": 1,
                   "final_bundle": 1},
           f"{fun}")

    # Integrity + reconstruction over stored payload.
    integrity = integrity_replay(stored)
    record("integrity", integrity["ok"], f"{integrity['errors']}")
    rebuilt = reconstruct_bundle(stored)
    record("reconstruction",
           rebuilt["matches"] is True
           and rebuilt["bundle_digest"] == stored["bundle"][
               "bundle_digest"],
           "rebuild mismatch")
    tampered = dict(stored)
    tampered["bundle"] = dict(stored["bundle"], content="forged")
    record("tamper",
           integrity_replay(tampered)["ok"] is False,
           "forgery undetected")

    # Counterfactual identity is distinct and marked.
    cid = counterfactual_id(stored["trace_id"], "trust",
                            {"version": "v2"}, "abc123")
    record("counterfactual_identity",
           cid != stored["trace_id"] and cid.startswith("ctx_"), cid)

    # Diff exposes stage changes.
    other = dict(stored)
    other["candidates"] = [
        dict(c, terminal_stage="TRUST_DENIED",
             final_selected=False) if c["candidate_id"] == "c1" else c
        for c in stored["candidates"]]
    other["bundle"] = dict(stored["bundle"], content="other",
                           bundle_digest=bundle_digest_for("other"))
    diff = diff_traces(stored, other)
    record("diff",
           diff["bundle_digest_changed"] is True
           and any(c["candidate_id"] == "c1"
                   for c in diff["changed_candidates"]),
           f"{diff['changed_candidates']}")

    total = sum(v["total"] for v in results.values())
    correct = sum(v["correct"] for v in results.values())
    return {"categories": results, "checks_total": total,
            "checks_passed": correct, "passed": total == correct,
            "eval_version": "trace-eval-v0.1"}
