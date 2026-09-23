# Bundled remembering engine (opencode-remembering product code).
"""Candidate lifecycle construction: one normalized record per
candidate encountered during context construction, preserving stage
boundaries. Stages that never ran on a candidate are recorded as
not_reached — never fabricated."""

from __future__ import annotations

from .canonical import content_hash
from .model import LIFECYCLE_SCHEMA_VERSION


def build_lifecycles(pool_items: list,
                     temporal_selected: set,
                     annotations: dict,
                     frame_kept: set | None,
                     frame_applied: bool,
                     trust_records: dict,
                     enforce_trust: bool,
                     selection_records: dict,
                     rendered_ids: list,
                     retrieval_index: dict,
                     trust_inputs: dict | None = None) -> list[dict]:
    """Assemble lifecycle records. retrieval_index maps chunk_id to
    {lexical_rank, dense_rank, fused_rank, paths, score}."""
    lifecycles: list[dict] = []
    rendered = set(rendered_ids)
    kept = frame_kept if frame_kept is not None else set()
    for item in pool_items:
        chunk_id = item["chunk_id"]
        source_id = item["source_id"]
        text = item.get("text", "")
        annotation = annotations.get(chunk_id)
        temporal_status = (annotation.status if annotation
                           else "not_modelled")
        in_temporal = chunk_id in temporal_selected
        record = trust_records.get(chunk_id)
        if frame_applied:
            frame_eligible = chunk_id in kept
            frame_info = {"eligible": frame_eligible,
                          "reason": ("frame.eligible" if frame_eligible
                                     else "frame.excluded")}
        else:
            frame_info = {"eligible": True, "reason": "frame.not_applied"}
        if record is not None:
            trust_info = {"verdict": record.to_dict()["verdict"],
                          "reason": record.to_dict()["reason"],
                          "stage": record.to_dict()["stage"]}
        else:
            trust_info = {"verdict": "not_reached",
                          "reason": "trust.not_evaluated", "stage": "none"}
        selection = selection_records.get(chunk_id)
        if selection is not None:
            selection_info = {
                "class": selection.selection_class.value,
                "disposition": selection.disposition.value,
                "reason": selection.reason,
                "selection_order": selection.selection_order,
            }
        else:
            selection_info = {"class": "not_reached",
                              "disposition": "not_reached",
                              "reason": "selection.not_evaluated",
                              "selection_order": -1}
        retrieval = retrieval_index.get(chunk_id, {})
        final_selected = chunk_id in rendered
        terminal_stage, terminal_reason = _terminal(
            in_temporal, frame_applied, frame_info["eligible"],
            trust_info, selection_info, final_selected, enforce_trust)
        lifecycles.append({
            "candidate_id": chunk_id,
            "source_id": source_id,
            "chunk_id": chunk_id,
            "content_hash": content_hash(text),
            "retrieved": True,
            "retrieval": {
                "paths": retrieval.get("paths", []),
                "lexical_rank": retrieval.get("lexical_rank"),
                "dense_rank": retrieval.get("dense_rank"),
                "fused_rank": retrieval.get("fused_rank"),
                "score": retrieval.get("score"),
            },
            "temporal": {
                "status": temporal_status,
                "selected": in_temporal,
                "reason": ("temporal.selected" if in_temporal
                           else "temporal.suppressed"),
            },
            "frame": frame_info,
            "trust": trust_info,
            "trust_input": (trust_inputs or {}).get(chunk_id, {}),
            "selection": selection_info,
            "final_selected": final_selected,
            "terminal_stage": terminal_stage,
            "terminal_reason": terminal_reason,
            "snapshot_text": text,
            "lifecycle_version": LIFECYCLE_SCHEMA_VERSION,
        })
    lifecycles.sort(key=lambda lifecycle: lifecycle["chunk_id"])
    return lifecycles


def _terminal(in_temporal: bool, frame_applied: bool,
              frame_eligible: bool, trust_info: dict,
              selection_info: dict, final_selected: bool,
              enforce_trust: bool) -> tuple[str, str]:
    if final_selected:
        return "FINAL_SELECTED", "selected into final bundle"
    if not in_temporal:
        return "TEMPORAL_SUPPRESSED", "temporal.suppressed_as_current"
    if frame_applied and not frame_eligible:
        return "FRAME_EXCLUDED", "frame.excluded"
    verdict = trust_info.get("verdict")
    if enforce_trust and verdict == "deny":
        return "TRUST_DENIED", trust_info.get("reason", "")
    if enforce_trust and verdict == "quarantine":
        return "TRUST_QUARANTINED", trust_info.get("reason", "")
    disposition = selection_info.get("disposition", "")
    if disposition == "drop_redundant":
        return "SELECTION_REDUNDANT", selection_info.get("reason", "")
    if disposition == "drop_low_value":
        return "SELECTION_LOW_VALUE", selection_info.get("reason", "")
    if disposition == "drop_budget" or not final_selected:
        return "SELECTION_BUDGET", selection_info.get(
            "reason", "drop.budget")
    return "UNKNOWN", "unrendered without recorded stage"


def funnel(lifecycles: list[dict]) -> dict:
    temporal = [c for c in lifecycles if c["temporal"]["selected"]]
    framed = [c for c in temporal if c["frame"]["eligible"]]
    framed_ids = {c["candidate_id"] for c in framed}
    admitted = [c for c in lifecycles
                if (c["trust"]["verdict"] == "admit"
                    or c["final_selected"])
                and c["candidate_id"] in framed_ids]
    selected = [c for c in lifecycles if c["final_selected"]]
    return {"retrieved": len(lifecycles),
            "temporal_survivors": len(temporal),
            "frame_survivors": len(framed),
            "admitted": len(admitted),
            "selected": len(selected),
            "final_bundle": len(selected)}
