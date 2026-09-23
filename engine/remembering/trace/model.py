# Bundled remembering engine (opencode-remembering product code).
"""Durable ContextTrace model: the immutable execution record for one
context-construction attempt. The trace observes the pipeline; it
never alters pipeline semantics."""

from __future__ import annotations

from dataclasses import dataclass, field

TRACE_SCHEMA_VERSION = "context-trace-v0.1"
TRACE_STORE_VERSION = "trace-store-v0.1"
CANONICAL_FORMAT_VERSION = "trace-canonical-json-v0.1"
REPLAY_PROTOCOL_VERSION = "trace-replay-v0.1"
LIFECYCLE_SCHEMA_VERSION = "candidate-lifecycle-v0.1"
TRACE_ENGINE_VERSION = "trace-engine-v0.1"


@dataclass(frozen=True)
class CandidateLifecycle:
    candidate_id: str
    source_id: str
    chunk_id: str
    content_hash: str
    retrieved: bool
    retrieval: dict = field(default_factory=dict)
    temporal: dict = field(default_factory=dict)
    frame: dict = field(default_factory=dict)
    trust: dict = field(default_factory=dict)
    selection: dict = field(default_factory=dict)
    final_selected: bool = False
    terminal_stage: str = "unknown"
    terminal_reason: str = ""
    snapshot_text: str = ""


@dataclass(frozen=True)
class ContextTrace:
    trace_schema_version: str
    trace_id: str
    project_id: str
    project_digest: str
    created_at: str
    request: dict
    route: dict
    versions: dict
    stages: dict
    candidates: tuple
    bundle: dict
    timings: dict
    integrity: dict
