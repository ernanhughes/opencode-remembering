# Bundled remembering engine (opencode-remembering product code).
"""PostgreSQL trace store: immutable context_traces plus candidate and
policy sidecars. One transaction per trace (complete or absent).
Same trace ID + identical payload is idempotent success; same ID +
different payload is TRACE_HASH_CONFLICT. No UPDATE path exists."""

from __future__ import annotations

import json

from .canonical import (
    bundle_digest_for,
    canonical_bytes,
    canonical_json,
    content_hash,
    identity_payload,
    input_digest_for,
    pool_digest_for,
    sha256_hex,
    trace_id_for,
    verification_payload,
)
from .model import TRACE_SCHEMA_VERSION, TRACE_STORE_VERSION

META_KEYS = {
    "trace.store_version": TRACE_STORE_VERSION,
    "trace.schema_version": TRACE_SCHEMA_VERSION,
}


def initialise(conn, schema: str) -> dict:
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.context_traces (
                    trace_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    project_digest TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    route TEXT NOT NULL,
                    temporal_mode TEXT,
                    work_type TEXT,
                    project_frame_version TEXT,
                    trust_policy_version TEXT,
                    selection_policy_version TEXT,
                    input_digest TEXT NOT NULL,
                    candidate_pool_digest TEXT,
                    bundle_digest TEXT NOT NULL,
                    trace_json JSONB NOT NULL,
                    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.context_trace_candidates (
                    trace_id TEXT NOT NULL REFERENCES {}.context_traces(trace_id)
                        ON DELETE CASCADE,
                    candidate_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    retrieved BOOLEAN NOT NULL DEFAULT TRUE,
                    temporal_status TEXT,
                    frame_eligible BOOLEAN,
                    trust_verdict TEXT,
                    selection_disposition TEXT,
                    final_selected BOOLEAN NOT NULL DEFAULT FALSE,
                    terminal_stage TEXT NOT NULL,
                    terminal_reason TEXT NOT NULL,
                    PRIMARY KEY (trace_id, candidate_id)
                )
                """
            ).format(s, s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.context_trace_policies (
                    trace_id TEXT NOT NULL REFERENCES {}.context_traces(trace_id)
                        ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    policy_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (trace_id, stage, policy_name)
                )
                """
            ).format(s, s)
        )
        for name, table, column in (
            ("context_traces_created_idx", "context_traces", "created_at"),
            ("context_traces_route_idx", "context_traces", "route"),
            ("candidates_source_idx", "context_trace_candidates",
             "source_id"),
            ("candidates_chunk_idx", "context_trace_candidates",
             "chunk_id"),
            ("candidates_terminal_idx", "context_trace_candidates",
             "terminal_stage"),
            ("policies_version_idx", "context_trace_policies",
             "version"),
        ):
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                    sql.Identifier(f"{schema}_{name}"),
                    s,
                    sql.Identifier(table),
                    sql.Identifier(column),
                )
            )
        for key, value in META_KEYS.items():
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(s),
                (key, value),
            )
    return {"store_version": TRACE_STORE_VERSION,
            "schema_version": TRACE_SCHEMA_VERSION}


def _storage_payload(trace: dict) -> dict:
    """Payload covered by the trace identity: everything semantic,
    excluding the identity itself, creation/storage timestamps, and
    all execution timings."""
    return identity_payload(trace)


def compute_ids(semantic: dict, bundle_content: str,
                pool_entries: list) -> dict:
    normalized = verification_payload(semantic)
    pool = normalized.get("candidate_pool", {})
    return {
        "trace_id": trace_id_for(normalized),
        "bundle_digest": bundle_digest_for(bundle_content),
        "candidate_pool_digest": pool.get("candidate_pool_digest"),
        "input_digest": input_digest_for(
            normalized.get("request", {})),
    }


def insert_trace(conn, schema: str, semantic: dict, bundle_content: str,
                 pool_entries: list, created_at: str,
                 lifecycles: list, policies: list) -> dict:
    """Insert one trace atomically. Idempotent on identical payload;
    TRACE_HASH_CONFLICT on same ID with different content."""
    import datetime

    from psycopg import sql

    s = sql.Identifier(schema)
    ids = compute_ids(semantic, bundle_content, pool_entries)
    trace_id = ids["trace_id"]
    semantic["trace_id"] = trace_id
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT trace_json FROM {}.context_traces "
                        "WHERE trace_id = %s").format(s),
                (trace_id,))
            row = cur.fetchone()
            if row is not None:
                stored = row[0]
                if canonical_json(_storage_payload(stored)) == \
                        canonical_json(identity_payload(semantic)):
                    return {"inserted": False, "duplicate": True,
                            "trace_id": trace_id, **ids}
                raise ValueError(
                    f"TRACE_HASH_CONFLICT:{trace_id}: trace ID maps to "
                    "different content; payloads are immutable")
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.context_traces "
                    "(trace_id, schema_version, project_id, project_digest, "
                    "created_at, route, temporal_mode, work_type, "
                    "project_frame_version, trust_policy_version, "
                    "selection_policy_version, input_digest, "
                    "candidate_pool_digest, bundle_digest, trace_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s)"
                ).format(s),
                (
                    trace_id, semantic.get("schema_version"), semantic.get(
                        "project", {}).get("project_id"),
                    semantic.get("project", {}).get("project_digest"),
                    created_at, semantic.get("route", {}).get("route"),
                    (semantic.get("stages", {}).get("temporal") or {}).get(
                        "mode"),
                    ((semantic.get("stages", {}).get("frame") or {}).get(
                        "establishment") or {}).get("work_type"),
                    (semantic.get("stages", {}).get("frame") or {}).get(
                        "project_frame_version"),
                    (semantic.get("stages", {}).get("trust") or {}).get(
                        "policy_version"),
                    (semantic.get("stages", {}).get("selection") or {}).get(
                        "policy_version"),
                    ids["input_digest"], ids["candidate_pool_digest"],
                    ids["bundle_digest"],
                    json.dumps(semantic),
                ),
            )
            for lifecycle in lifecycles:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {}.context_trace_candidates "
                        "(trace_id, candidate_id, source_id, chunk_id, "
                        "content_hash, retrieved, temporal_status, "
                        "frame_eligible, trust_verdict, "
                        "selection_disposition, final_selected, "
                        "terminal_stage, terminal_reason) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s)"
                    ).format(s),
                    (
                        trace_id, lifecycle["candidate_id"],
                        lifecycle["source_id"], lifecycle["chunk_id"],
                        lifecycle["content_hash"],
                        lifecycle.get("retrieved", True),
                        (lifecycle.get("temporal") or {}).get("status"),
                        (lifecycle.get("frame") or {}).get("eligible"),
                        (lifecycle.get("trust") or {}).get("verdict"),
                        (lifecycle.get("selection") or {}).get(
                            "disposition"),
                        lifecycle.get("final_selected", False),
                        lifecycle.get("terminal_stage", "unknown"),
                        lifecycle.get("terminal_reason", ""),
                    ),
                )
            for policy in policies:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {}.context_trace_policies "
                        "(trace_id, stage, policy_name, version, digest) "
                        "VALUES (%s, %s, %s, %s, %s)"
                    ).format(s),
                    (
                        trace_id, policy["stage"], policy["policy_name"],
                        policy["version"], policy["digest"],
                    ),
                )
    return {"inserted": True, "duplicate": False, "trace_id": trace_id,
            **ids}


def fetch_trace(conn, schema: str, trace_id: str) -> dict | None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT trace_json FROM {}.context_traces "
                    "WHERE trace_id = %s").format(
                        sql.Identifier(schema)),
            (trace_id,))
        row = cur.fetchone()
        return row[0] if row is not None else None


def verify_trace(conn, schema: str, trace_id: str) -> dict:
    """Recompute identity, pool, and bundle digests from storage."""
    stored = fetch_trace(conn, schema, trace_id)
    if stored is None:
        return {"trace_id": trace_id, "found": False, "ok": False,
                "errors": ["TRACE_NOT_FOUND"]}
    errors: list[str] = []
    normalized = verification_payload(stored)
    pool = stored.get("candidate_pool", {})
    if trace_id_for(normalized) != trace_id:
        errors.append("TRACE_ID_MISMATCH")
    bundle = stored.get("bundle", {})
    content = bundle.get("content", "")
    if bundle_digest_for(content) != bundle.get("bundle_digest"):
        errors.append("BUNDLE_DIGEST_MISMATCH")
    if pool_digest_for(normalized["candidate_pool"]["entries"]) != \
            pool.get("candidate_pool_digest"):
        errors.append("POOL_DIGEST_MISMATCH")
    return {"trace_id": trace_id, "found": True,
            "ok": not errors, "errors": errors}


def trace_count(conn, schema: str) -> dict:
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL("SELECT count(*), min(created_at), "
                        "max(created_at) FROM {}.context_traces").format(s))
            row = cur.fetchone()
        except Exception:
            return {"traces": 0, "oldest": None, "newest": None,
                    "ready": False}
        return {"traces": int(row[0]),
                "oldest": row[1].isoformat() if row[1] else None,
                "newest": row[2].isoformat() if row[2] else None,
                "ready": True}


def versions(conn, schema: str) -> dict:
    from psycopg import sql

    out = {"store_version": None, "schema_version": None}
    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL("SELECT key, value FROM {}.meta "
                        "WHERE key LIKE 'trace.%%'").format(
                    sql.Identifier(schema)))
            for key, value in cur.fetchall():
                short = key.split(".", 1)[1]
                if short in out:
                    out[short] = value
        except Exception:
            pass
    return out
