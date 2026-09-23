# Bundled remembering engine (opencode-remembering product code).
"""Trace lookup: exact fetch, filtered summaries, source/chunk/policy
lookup, deterministic candidate explanation. Projections over stored
traces — never LLM-generated."""

from __future__ import annotations


def summarize(trace: dict) -> dict:
    bundle = trace.get("bundle", {})
    funnel_counts = trace.get("funnel", {})
    versions = trace.get("versions", {})
    route = trace.get("route", {})
    return {
        "trace_id": trace.get("trace_id"),
        "created_at": trace.get("created_at"),
        "route": route.get("route"),
        "work_type": ((trace.get("stages", {}).get("frame") or {}).get(
            "establishment") or {}).get("work_type"),
        "bundle_digest": bundle.get("bundle_digest"),
        "candidate_count": len(trace.get("candidates", [])),
        "selected_count": sum(
            1 for c in trace.get("candidates", [])
            if c.get("final_selected")),
        "policy_versions": versions,
        "funnel": funnel_counts,
    }


def find_traces(conn, schema: str, filters: dict) -> list[dict]:
    """Filtered trace summaries. Supported filters: source_id,
    chunk_id, route, work_type, policy_stage, policy_version,
    terminal_stage, before, after, limit."""
    from psycopg import sql

    s = sql.Identifier(schema)
    limit = filters.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("TRACE_INVALID_FILTER:limit must be an integer")
    limit = max(1, min(limit, 100))

    source_id = filters.get("source_id")
    chunk_id = filters.get("chunk_id")
    route = filters.get("route")
    work_type = filters.get("work_type")
    policy_stage = filters.get("policy_stage")
    policy_version = filters.get("policy_version")
    terminal_stage = filters.get("terminal_stage")
    before = filters.get("before")
    after = filters.get("after")

    conditions: list = []
    params: list = []
    joins: list[str] = []
    if source_id is not None:
        joins.append(
            "JOIN {}.context_trace_candidates cand ON "
            "cand.trace_id = t.trace_id".format(schema))
        conditions.append("cand.source_id = %s")
        params.append(source_id)
    if chunk_id is not None:
        if not joins:
            joins.append(
                "JOIN {}.context_trace_candidates cand ON "
                "cand.trace_id = t.trace_id".format(schema))
        else:
            joins[0] += ""
        conditions.append("cand.chunk_id = %s")
        params.append(chunk_id)
    if terminal_stage is not None:
        if not joins:
            joins.append(
                "JOIN {}.context_trace_candidates cand ON "
                "cand.trace_id = t.trace_id".format(schema))
        conditions.append("cand.terminal_stage = %s")
        params.append(terminal_stage)
    if route is not None:
        conditions.append("t.route = %s")
        params.append(route)
    if work_type is not None:
        conditions.append("t.work_type = %s")
        params.append(work_type)
    if before is not None:
        conditions.append("t.created_at < %s")
        params.append(before)
    if after is not None:
        conditions.append("t.created_at >= %s")
        params.append(after)

    policy_join = ""
    if policy_stage is not None or policy_version is not None:
        policy_join = ("JOIN {}.context_trace_policies pol ON "
                       "pol.trace_id = t.trace_id".format(schema))
        if policy_stage is not None:
            conditions.append(
                "(pol.stage = %s OR pol.policy_name = %s)")
            params.extend([policy_stage, policy_stage])
        if policy_version is not None:
            conditions.append("pol.version = %s")
            params.append(policy_version)

    query = (
        "SELECT t.trace_id, t.trace_json FROM {}.context_traces t "
        + " ".join(joins)
        + (" " + policy_join if policy_join else "")
        + (" WHERE " + " AND ".join(conditions) if conditions else "")
        + " ORDER BY t.created_at DESC LIMIT %s"
    )
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql.SQL(query).format(s), params)
        seen: set[str] = set()
        out = []
        for trace_id, blob in cur.fetchall():
            if trace_id in seen:
                continue
            seen.add(trace_id)
            summary = summarize(blob)
            summary["trace_id"] = trace_id
            out.append(summary)
        return out


def explain_candidate(trace: dict, candidate_id: str) -> dict:
    """Deterministic candidate explanation: staged path with reasons."""
    for candidate in trace.get("candidates", []):
        if candidate.get("candidate_id") != candidate_id:
            continue
        retrieval = candidate.get("retrieval", {})
        temporal = candidate.get("temporal", {})
        frame = candidate.get("frame", {})
        trust = candidate.get("trust", {})
        selection = candidate.get("selection", {})
        return {
            "candidate_id": candidate_id,
            "source_id": candidate.get("source_id"),
            "chunk_id": candidate.get("chunk_id"),
            "retrieval": {
                "paths": retrieval.get("paths", []),
                "lexical_rank": retrieval.get("lexical_rank"),
                "dense_rank": retrieval.get("dense_rank"),
                "fused_rank": retrieval.get("fused_rank"),
            },
            "temporal": {
                "status": temporal.get("status"),
                "selected": temporal.get("selected"),
                "reason": temporal.get("reason"),
            },
            "frame": {
                "eligible": frame.get("eligible"),
                "reason": frame.get("reason"),
            },
            "trust": {
                "verdict": trust.get("verdict"),
                "reason": trust.get("reason"),
                "stage": trust.get("stage"),
            },
            "selection": {
                "class": selection.get("class"),
                "disposition": selection.get("disposition"),
                "reason": selection.get("reason"),
            },
            "terminal_stage": candidate.get("terminal_stage"),
            "terminal_reason": candidate.get("terminal_reason"),
            "final_selected": candidate.get("final_selected"),
        }
    return {"candidate_id": candidate_id, "found": False}
