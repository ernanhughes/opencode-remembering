# Bundled remembering engine (opencode-remembering product code).
"""Replay, precisely separated:

- integrity replay: recompute stored hashes (no policy execution);
- reconstruction: rebuild the bundle from stored snapshots (no live
  access, no embeddings);
- counterfactual policy replay: trust and/or selection policies over
  the frozen recorded evidence boundary (never fresh retrieval).

Replays never mutate the source trace. Counterfactual results get
their own content-addressed identity marked as replays.
"""

from __future__ import annotations

from .canonical import (
    bundle_digest_for,
    canonical_json,
    pool_digest_for,
    sha256_hex,
    trace_id_for,
)
from .model import REPLAY_PROTOCOL_VERSION


def integrity_replay(stored: dict) -> dict:
    """Recompute identity, pool, and bundle digests from storage."""
    from .canonical import verification_payload

    errors: list[str] = []
    normalized = verification_payload(stored)
    pool = stored.get("candidate_pool", {})
    if trace_id_for(normalized) != stored.get("trace_id"):
        errors.append("TRACE_ID_MISMATCH")
    bundle = stored.get("bundle", {})
    if bundle_digest_for(bundle.get("content", "")) != bundle.get(
            "bundle_digest"):
        errors.append("BUNDLE_DIGEST_MISMATCH")
    pool = stored.get("candidate_pool", {})
    if pool_digest_for(pool.get("entries", [])) != pool.get(
            "candidate_pool_digest"):
        errors.append("POOL_DIGEST_MISMATCH")
    return {"ok": not errors, "errors": errors,
            "replay_protocol": REPLAY_PROTOCOL_VERSION}


def reconstruct_bundle(stored: dict) -> dict:
    """Rebuild the final bundle from stored snapshots only."""
    bundle = stored.get("bundle", {})
    content = bundle.get("content", "")
    digest = bundle_digest_for(content)
    return {
        "content": content,
        "chars": bundle.get("chars", len(content)),
        "order": bundle.get("render_order", []),
        "bundle_digest": digest,
        "matches": digest == bundle.get("bundle_digest"),
    }


def trust_replay_inputs(stored: dict) -> dict:
    """Extract the frozen Stage-5 input boundary from a trace."""
    pool = stored.get("candidate_pool", {})
    entries = pool.get("entries", [])
    if not entries:
        return {"ok": False, "error": "REPLAY_INPUT_INSUFFICIENT",
                "missing": ["candidate_pool.entries"]}
    return {"ok": True, "entries": entries}


def selection_replay_inputs(stored: dict) -> dict:
    """Extract the frozen admitted pool for selection replay."""
    admitted = [c for c in stored.get("candidates", [])
                if c.get("trust", {}).get("verdict") == "admit"
                or c.get("final_selected")]
    if not admitted:
        return {"ok": False, "error": "REPLAY_INPUT_INSUFFICIENT",
                "missing": ["admitted candidates"]}
    return {"ok": True, "entries": admitted}


def counterfactual_id(source_trace_id: str, kind: str,
                      policy_identity: dict, result_digest: str) -> str:
    material = {
        "replay_of": source_trace_id,
        "replay_kind": kind,
        "policy": policy_identity,
        "result_digest": result_digest,
    }
    return "ctx_" + sha256_hex(
        canonical_json(material).encode("utf-8"))[:32]


def diff_traces(old: dict, new: dict) -> dict:
    """Structured diff between two traces (original vs replay)."""
    def verdicts(trace):
        return {c["candidate_id"]: (c.get("trust", {}).get("verdict"),
                                    c.get("terminal_stage"))
                for c in trace.get("candidates", [])}

    old_v, new_v = verdicts(old), verdicts(new)
    changed = sorted(
        cid for cid in set(old_v) | set(new_v)
        if old_v.get(cid) != new_v.get(cid))
    old_sel = [c["candidate_id"] for c in old.get("candidates", [])
               if c.get("final_selected")]
    new_sel = [c["candidate_id"] for c in new.get("candidates", [])
               if c.get("final_selected")]
    return {
        "source_trace_id": old.get("trace_id"),
        "compared_trace_id": new.get("trace_id"),
        "policy_identities": {
            "old": old.get("versions", {}),
            "new": new.get("versions", {}),
        },
        "changed_candidates": [
            {"candidate_id": cid, "old": old_v.get(cid),
             "new": new_v.get(cid)} for cid in changed],
        "old_selected_ids": old_sel,
        "new_selected_ids": new_sel,
        "bundle_digest_changed": old.get("bundle", {}).get(
            "bundle_digest") != new.get("bundle", {}).get("bundle_digest"),
        "bundle_chars": {
            "old": old.get("bundle", {}).get("chars"),
            "new": new.get("bundle", {}).get("chars"),
        },
    }
