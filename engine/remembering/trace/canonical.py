# Bundled remembering engine (opencode-remembering product code).
"""Canonical serialization and content addressing. One serializer with
explicit rules: UTF-8, sorted object keys, compact separators, ISO
timestamps as given, no NaN/Infinity, None as null. Latencies and
storage timestamps are excluded from identity hashes by the caller
(they are execution evidence, not semantic decisions)."""

from __future__ import annotations

import hashlib
import json

from .model import CANONICAL_FORMAT_VERSION


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(value) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trace_id_for(semantic_payload: dict) -> str:
    return "ctx_" + sha256_hex(canonical_bytes(semantic_payload))[:32]


_VOLATILE_KEYS = frozenset({
    "timings", "inserted_at", "trace_id", "created_at",
    "latencies_ms", "latency_ms", "trace_construction_ms",
    "trace_persistence_ms", "total_context_ms",
    "work_frame_id", "as_of",
})


def normalize_for_identity(value):
    """Strip execution-volatile fields (timings, storage stamps) so
    semantically identical executions share an identity. Rank lists,
    decisions, and policy identities are semantic and retained."""
    if isinstance(value, dict):
        return {key: normalize_for_identity(item)
                for key, item in value.items()
                if key not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [normalize_for_identity(item) for item in value]
    return value


def identity_payload(semantic: dict) -> dict:
    return normalize_for_identity(semantic)


def verification_payload(stored: dict) -> dict:
    """Normalized payload plus order-independent pool, for identity
    recomputation. Single rule shared by insert, verify, and replay."""
    normalized = identity_payload(stored)
    pool = stored.get("candidate_pool", {})
    normalized["candidate_pool"] = {
        "entries": sorted(pool.get("entries", []),
                          key=lambda entry: entry.get("chunk_id", "")),
        "candidate_pool_digest": pool.get("candidate_pool_digest"),
    }
    return normalized


def content_hash(text: str) -> str:
    return sha256_hex((text or "").encode("utf-8"))[:16]


def bundle_digest_for(rendered_content: str) -> str:
    return sha256_hex(rendered_content.encode("utf-8"))


def pool_digest_for(entries: list) -> str:
    """Ordered candidate identities + content hashes entering a
    selection boundary."""
    return sha256_hex(canonical_bytes(entries))


def input_digest_for(request: dict) -> str:
    return sha256_hex(canonical_bytes(request))[:16]


def format_version() -> str:
    return CANONICAL_FORMAT_VERSION
