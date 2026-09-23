#!/usr/bin/env python3
"""Temporary process boundary between OpenCode and Project Memory.

This bridge intentionally contains no memory policy of its own. It imports
Project Memory's PostgreSQL store and exposes a tiny JSON-over-stdin command
surface until Project Memory grows a stable service/API.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("bridge input must be a JSON object")
    return value


def require_project_memory() -> Path:
    value = os.environ.get("PROJECT_MEMORY_ROOT", "").strip()
    if not value:
        raise RuntimeError("PROJECT_MEMORY_ROOT is required")
    root = Path(value).expanduser().resolve()
    solution = root / "solution"
    baseline = solution / "memory_baseline" / "storage.py"
    if not baseline.is_file():
        raise RuntimeError(
            f"Project Memory checkout is missing expected file: {baseline}"
        )
    sys.path.insert(0, str(solution))
    return root


def dsn() -> str:
    return os.environ.get(
        "MEMORY_BASELINE_DSN",
        "postgresql://postgres:postgres@localhost:5434/memory_baseline",
    )


def redact_dsn(value: str) -> str:
    # Keep this deliberately simple: never echo credentials from a configured DSN.
    if "@" not in value:
        return value
    prefix, suffix = value.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "postgresql"
    return f"{scheme}://***:***@{suffix}"


def schema_from(payload: dict[str, Any]) -> str:
    value = payload.get("schema")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("schema is required")
    return value.strip()


def doctor(payload: dict[str, Any]) -> dict[str, Any]:
    root = require_project_memory()

    import psycopg

    schema = schema_from(payload)
    connection = psycopg.connect(dsn(), autocommit=True)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(
                    "pgvector is required but CREATE EXTENSION vector has not been run"
                )
            vector_version = str(row[0])

            cur.execute("SELECT to_regclass(%s)", (f"{schema}.chunks",))
            indexed = cur.fetchone()[0] is not None
            chunks: int | None = None
            if indexed:
                from psycopg import sql

                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.chunks").format(
                        sql.Identifier(schema)
                    )
                )
                chunks = int(cur.fetchone()[0])
    finally:
        connection.close()

    # Import the actual implementation as part of the prerequisite check.
    from memory_baseline.storage import Store  # noqa: F401

    return {
        "ok": True,
        "project_memory_root": str(root),
        "dsn_redacted": redact_dsn(dsn()),
        "schema": schema,
        "pgvector_version": vector_version,
        "indexed": indexed,
        "chunks": chunks,
    }


def lexical_search(payload: dict[str, Any]) -> dict[str, Any]:
    require_project_memory()
    schema = schema_from(payload)
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    limit = payload.get("limit", 8)
    if not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    limit = max(1, min(limit, 20))

    health = doctor(payload)
    if not health["indexed"]:
        return {
            "ok": True,
            "indexed": False,
            "schema": schema,
            "items": [],
            "message": (
                "This project's Project Memory schema has not been indexed yet. "
                "Run the ingestion/refresh milestone described in HANDOFF.md."
            ),
        }

    from memory_baseline.storage import Store

    store = Store(dsn(), schema)
    try:
        chunks = store.lexical_search(query.strip(), limit)
    finally:
        store.close()

    return {
        "ok": True,
        "indexed": True,
        "schema": schema,
        "items": [
            {
                "chunk_id": item.chunk_id,
                "source_id": item.source_id,
                "section": item.section,
                "rank": item.rank,
                "score": item.score,
                "text": item.text,
            }
            for item in chunks
        ],
    }


def context(payload: dict[str, Any]) -> dict[str, Any]:
    max_chars = payload.get("max_chars", 4000)
    max_results = payload.get("max_results", 6)
    if not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(max_results, int) or max_results < 1:
        raise ValueError("max_results must be a positive integer")

    search_payload = dict(payload)
    search_payload["limit"] = min(max_results, 20)
    result = lexical_search(search_payload)

    if not result["indexed"]:
        return {
            **result,
            "trace_id": "bootstrap:unindexed",
            "content": "",
            "chars": 0,
        }

    parts: list[str] = []
    admitted: list[dict[str, Any]] = []
    chars = 0

    for item in result["items"]:
        section = f" | {item['section']}" if item["section"] else ""
        piece = (
            f"[source: {item['source_id']}{section} | rank: {item['rank']}]\n"
            f"{item['text']}"
        )
        separator = "\n\n---\n\n" if parts else ""
        added = len(separator) + len(piece)
        if chars + added > max_chars:
            break
        parts.append(piece)
        admitted.append(item)
        chars += added

    trace_material = json.dumps(
        {
            "schema": result["schema"],
            "query": payload.get("query", ""),
            "chunks": [item["chunk_id"] for item in admitted],
        },
        sort_keys=True,
    )
    trace_id = "bootstrap:" + hashlib.sha256(
        trace_material.encode("utf-8")
    ).hexdigest()[:16]

    return {
        "ok": True,
        "indexed": True,
        "schema": result["schema"],
        "items": admitted,
        "trace_id": trace_id,
        "content": "\n\n---\n\n".join(parts),
        "chars": chars,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: remembering_bridge.py <doctor|search|context>")

    action = sys.argv[1]
    payload = read_payload()

    if action == "doctor":
        result = doctor(payload)
    elif action == "search":
        result = lexical_search(payload)
    elif action == "context":
        result = context(payload)
    else:
        raise ValueError(f"unknown bridge action: {action}")

    emit(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # Fail loudly and keep stdout parseable.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise
