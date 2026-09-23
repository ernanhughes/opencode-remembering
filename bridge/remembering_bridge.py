#!/usr/bin/env python3
"""Process boundary between OpenCode and the bundled remembering engine.

This bridge contains no memory policy of its own. It imports the
plugin's bundled engine (engine/remembering: PostgreSQL store, ingestion, embedding, retrieval, routing
and context modules and exposes a small JSON-over-stdin command
surface:

    doctor | setup | refresh | search | context | capture_session
    | route_eval | temporal_import | state | temporal_eval
    | frame_eval | trust_import | trust_eval | selection_eval
    | trace | trace_eval

Retrieval finds evidence; later policy stages decide whether evidence
may influence present action. Nothing here judges temporal validity,
framing, trust, or open loops — those arrive in later stages.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


# -- errors -----------------------------------------------------------

class BridgeError(Exception):
    """Coded failure with an actionable message (never contains secrets)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


# -- plumbing ----------------------------------------------------------

def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise BridgeError("CONFIG_INVALID", "bridge input must be a JSON object")
    return value


def require_engine() -> Path:
    """Locate the bundled engine relative to this file and import it.

    No environment variables, no sibling-directory search, no
    sys.path mutation toward another repository. A missing engine is
    a packaging error and fails visibly; there is no fallback.
    """
    engine_dir = Path(__file__).resolve().parent.parent / "engine"
    package = engine_dir / "remembering" / "__init__.py"
    if not package.is_file():
        raise BridgeError(
            "ENGINE_MISSING",
            f"bundled remembering engine not found at {engine_dir}; "
            "the plugin installation is incomplete.",
        )
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    try:
        from remembering import ENGINE_VERSION  # noqa: F401
        from remembering.baseline import storage  # noqa: F401
        from remembering.temporal import model  # noqa: F401
    except ImportError as exc:
        raise BridgeError(
            "ENGINE_MISSING",
            f"bundled remembering engine failed to import: {exc}",
        )
    return engine_dir


def dsn() -> str:
    return os.environ.get(
        "MEMORY_BASELINE_DSN",
        "postgresql://postgres:postgres@localhost:5434/memory",
    )


def redact_dsn(value: str) -> str:
    # Never echo credentials from a configured DSN.
    if "@" not in value:
        return value
    prefix, suffix = value.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "postgresql"
    return f"{scheme}://***:***@{suffix}"


def schema_from(payload: dict[str, Any]) -> str:
    value = payload.get("schema")
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("CONFIG_INVALID", "schema is required")
    schema = value.strip()
    # Fail closed: never silently substitute a global schema.
    if not SCHEMA_PATTERN.match(schema) or len(schema) > 63:
        raise BridgeError(
            "CONFIG_INVALID",
            f"refusing unsafe schema name {schema!r}; refusing to fall back "
            "to a shared schema.",
        )
    if schema.lower().startswith("pg_"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"refusing reserved schema name {schema!r}.",
        )
    return schema


def project_directory_from(payload: dict[str, Any]) -> Path:
    value = payload.get("project_directory")
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("CONFIG_INVALID", "project_directory is required")
    return Path(value.strip())


def canonical_project_dir(directory: Path) -> str:
    """Same canonicalization the TypeScript adapter uses for schema identity.

    realpath + lowercase-on-Windows so the same repository path always
    resolves to the same identity string on every platform.
    """
    resolved = os.path.realpath(directory)
    if sys.platform == "win32":
        resolved = resolved.lower()
    return resolved


def embedding_spec_from(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("embedding") or {}
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID", "embedding must be an object")
    provider = str(raw.get("provider", "ollama"))
    model = str(raw.get("model", "bge-m3"))
    host = str(raw.get("host", "http://localhost:11434")).rstrip("/")
    if provider not in ("ollama", "sentence-transformers", "hashing"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown embedding provider {provider!r}; refusing to guess.",
        )
    if provider == "hashing":
        # Deterministic test double. Never a production retrieval model:
        # only honoured for explicitly opted-in local tests.
        if os.environ.get("REMEMBERING_ALLOW_TEST_EMBEDDINGS") != "1":
            raise BridgeError(
                "CONFIG_INVALID",
                "the hashing embedder is a test double and is refused "
                "without REMEMBERING_ALLOW_TEST_EMBEDDINGS=1.",
            )
    if not model.strip():
        raise BridgeError("CONFIG_INVALID", "embedding.model must not be empty")
    dimension = raw.get("dimension", 64)
    return {"provider": provider, "model": model.strip(), "host": host,
            "dimension": dimension}


def retrieval_spec_from(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("retrieval") or {}
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID", "retrieval must be an object")
    mode = str(raw.get("mode", "hybrid"))
    if mode not in ("lexical", "dense", "hybrid"):
        raise BridgeError("CONFIG_INVALID", f"unknown retrieval mode {mode!r}")
    reranker = str(raw.get("reranker", "none"))
    if reranker not in ("none", "overlap", "cross-encoder"):
        raise BridgeError("CONFIG_INVALID", f"unknown reranker {reranker!r}")

    def integer(name: str, default: int, low: int, high: int) -> int:
        value = raw.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise BridgeError("CONFIG_INVALID", f"retrieval.{name} must be int")
        if not (low <= value <= high):
            raise BridgeError(
                "CONFIG_INVALID",
                f"retrieval.{name} must be between {low} and {high}",
            )
        return value

    return {
        "mode": mode,
        "lexical_k": integer("lexical_k", 30, 1, 100),
        "dense_k": integer("dense_k", 30, 1, 100),
        "fusion_k": integer("fusion_k", 60, 1, 1000),
        "rerank_k": integer("rerank_k", 8, 1, 50),
        "reranker": reranker,
    }


def build_embedder(spec: dict[str, Any]):
    from remembering.baseline.embeddings import (
        HashingEmbedder,
        OllamaEmbeddingProvider,
        SentenceTransformerProvider,
    )

    provider = spec["provider"]
    if provider == "ollama":
        return OllamaEmbeddingProvider(spec["model"], spec["host"])
    if provider == "sentence-transformers":
        return SentenceTransformerProvider(spec["model"])
    if provider == "hashing":
        dimension = spec.get("dimension", 64)
        if not isinstance(dimension, int):
            raise BridgeError("CONFIG_INVALID",
                              "embedding.dimension must be int")
        return HashingEmbedder(dimension)
    raise BridgeError("CONFIG_INVALID", f"unknown provider {provider!r}")


def build_baseline_config(schema: str, emb: dict[str, Any],
                           ret: dict[str, Any]):
    from remembering.baseline.config import (
        BaselineConfig,
        ContextConfig,
        EmbeddingConfig,
        RetrievalConfig,
    )

    return BaselineConfig(
        dsn=dsn(),
        schema=schema,
        embedding=EmbeddingConfig(
            provider=emb["provider"], model=emb["model"],
            ollama_host=emb["host"],
        ),
        retrieval=RetrievalConfig(
            mode=ret["mode"], lexical_k=ret["lexical_k"],
            dense_k=ret["dense_k"], fusion_k=ret["fusion_k"],
            reranker=ret["reranker"], rerank_k=ret["rerank_k"],
        ),
        context=ContextConfig(),
    )


# -- PostgreSQL pre-checks ----------------------------------------------

def classify_connection_error(exc: Exception) -> BridgeError:
    message = str(exc)
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "3D000" or "does not exist" in message and "database" in message:
        return BridgeError(
            "DB_MISSING",
            "PostgreSQL server is reachable, but the configured database "
            "does not exist. Create the database or change the configured "
            "DSN. Tables and schemas are created by memory_setup; the "
            "database itself is not.",
        )
    return BridgeError(
        "DB_UNREACHABLE",
        f"cannot reach PostgreSQL at {redact_dsn(dsn())}: "
        f"{type(exc).__name__}: {message[:200]}",
    )


def raw_connect():
    import psycopg

    try:
        return psycopg.connect(dsn(), autocommit=True, connect_timeout=10)
    except Exception as exc:
        raise classify_connection_error(exc)


def extension_status(cur, name: str) -> dict[str, Any]:
    cur.execute(
        "SELECT extversion FROM pg_extension WHERE extname = %s", (name,)
    )
    row = cur.fetchone()
    if row is not None:
        return {"installed": True, "version": str(row[0])}
    cur.execute(
        "SELECT default_version FROM pg_available_extensions "
        "WHERE name = %s",
        (name,),
    )
    row = cur.fetchone()
    if row is None:
        return {"installed": False, "version": None, "software": False}
    return {"installed": False, "version": None, "software": True,
            "default_version": str(row[0])}


def ensure_extension(cur, name: str) -> dict[str, Any]:
    """Enable one extension, distinguishing the three failure modes."""
    status = extension_status(cur, name)
    if status["installed"]:
        return status
    if not status.get("software"):
        raise BridgeError(
            "EXTENSION_UNAVAILABLE",
            f"extension {name!r} is not installed in this PostgreSQL "
            "installation (pg_available_extensions has no such entry). "
            "Install the extension software first "
            "(e.g. the pgvector package for your server), then retry.",
        )
    try:
        cur.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
    except Exception as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate == "42501" or "permission denied" in str(exc).lower():
            raise BridgeError(
                "EXTENSION_PERMISSION",
                f"permission denied enabling extension {name!r}. Ask a "
                f"database superuser to run CREATE EXTENSION {name}; the "
                "configured role cannot enable it.",
            )
        raise BridgeError(
            "EXTENSION_UNAVAILABLE",
            f"could not enable extension {name!r}: "
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    return extension_status(cur, name)


# -- embedding pre-checks -------------------------------------------------

def check_ollama_model(host: str, model: str) -> dict[str, Any]:
    """Distinguish host-down from model-missing; never download anything."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=10) as resp:
            payload = json.load(resp)
    except Exception as exc:
        raise BridgeError(
            "EMBEDDING_UNREACHABLE",
            f"embedding provider Ollama is not reachable at {host}: "
            f"{type(exc).__name__}: {str(exc)[:160]}",
        )
    names = [m.get("name", "") for m in payload.get("models", [])]
    wanted = {model, f"{model}:latest"}
    if not any(n in wanted or n.startswith(model + ":") for n in names):
        raise BridgeError(
            "EMBEDDING_MODEL_MISSING",
            f"embedding model {model!r} is not available in Ollama at "
            f"{host}. Run `ollama pull {model}` with the exact configured "
            "model name; the adapter will not download or substitute "
            "another model.",
        )
    return {"reachable": True, "available": True,
            "detail": f"ollama model {model!r} present at {host}"}


def check_embedding(spec: dict[str, Any]) -> dict[str, Any]:
    if spec["provider"] == "ollama":
        return check_ollama_model(spec["host"], spec["model"])
    if spec["provider"] == "sentence-transformers":
        try:
            import sentence_transformers  # noqa: F401
        except Exception as exc:
            raise BridgeError(
                "EMBEDDING_UNREACHABLE",
                "sentence-transformers is not importable in the bridge "
                f"Python environment: {str(exc)[:160]}",
            )
        return {"reachable": True, "available": True,
                "detail": f"sentence-transformers model {spec['model']!r}"}
    return {"reachable": True, "available": True,
            "detail": "hashing test double (REMEMBERING_ALLOW_TEST_EMBEDDINGS)"}


# -- project identity -----------------------------------------------------

META_KEYS = ("project.path", "project.schema")


def read_project_meta(cur, schema: str) -> dict[str, str]:
    from psycopg import sql

    try:
        cur.execute(
            sql.SQL("SELECT key, value FROM {}.meta").format(
                sql.Identifier(schema))
        )
    except Exception:
        return {}
    return {row[0]: row[1] for row in cur.fetchall()}


def ensure_temporal_objects(connection, schema: str) -> dict[str, Any]:
    """Create temporal storage (idempotent). Versions are recorded in
    the schema's meta table; existing rows are never migrated."""
    from remembering.temporal import postgres as temporal_pg

    return temporal_pg.initialise(connection, schema)


def temporal_health(connection, schema: str) -> dict[str, Any]:
    """Compact temporal section for memory_health. Zero events is a
    healthy empty temporal store, not a failure."""
    from remembering.temporal import postgres as temporal_pg

    section: dict[str, Any] = {
        "store_ready": False,
        "store_version": None,
        "event_schema_version": None,
        "reducer_version": None,
        "events": 0,
        "subjects": [],
        "unknown_references": [],
        "causality_violations": [],
        "sequence_gaps": [],
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.temporal_events",))
            if cur.fetchone()[0] is None:
                return section
        section.update(temporal_pg.versions(connection, schema))
        section["store_ready"] = True
        section["events"] = temporal_pg.event_count(connection, schema)
        section["subjects"] = temporal_pg.subjects(connection, schema)
        if section["events"]:
            log = temporal_pg.load_log(connection, schema)
            section["unknown_references"] = log.unknown_refs()
            section["causality_violations"] = log.causality_violations()
            section["sequence_gaps"] = log.gaps()
    except Exception as exc:
        section["error"] = (f"{type(exc).__name__}: {str(exc)[:160]}")
    return section


TEMPORAL_EVENTS_REL = Path(".remembering") / "temporal" / "events.jsonl"


def import_temporal_events(connection, schema: str,
                           project_dir: Path) -> dict[str, Any]:
    """Deterministic import of explicit structured EventEnvelopes.

    Each line of .remembering/temporal/events.jsonl carries one
    envelope (plus optional received_at recorder metadata). Envelopes
    are validated by the existing temporal model; malformed lines
    fail visibly per line; exact replays are idempotent; conflicting
    identity reuse fails loudly. Nothing is silently repaired.
    """
    from remembering.temporal import postgres as temporal_pg
    from remembering.temporal.model import EventEnvelope

    report: dict[str, Any] = {"imported": 0, "duplicates": 0,
                              "failed": [], "events": 0, "subjects": []}
    path = Path(os.path.realpath(project_dir)) / TEMPORAL_EVENTS_REL
    if not path.is_file():
        return report
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report["failed"].append(f"{path}: unreadable: {exc}")
        return report
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            report["failed"].append(f"line {lineno}: not JSON: {exc}")
            continue
        if not isinstance(raw, dict):
            report["failed"].append(f"line {lineno}: must be an object")
            continue
        received_at = raw.get("received_at")
        if not isinstance(received_at, str) or not received_at.strip():
            received_at = datetime.datetime.now(
                datetime.timezone.utc).isoformat()
        try:
            envelope = EventEnvelope.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            report["failed"].append(
                f"line {lineno}: TEMPORAL_EVENT_INVALID:malformed "
                f"envelope: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        try:
            out = temporal_pg.append_event(connection, schema, envelope,
                                           received_at)
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            report["failed"].append(f"line {lineno}: {code}:"
                                    f"{envelope.event_id}")
            continue
        if out.get("duplicate"):
            report["duplicates"] += 1
        else:
            report["imported"] += 1
    report["events"] = temporal_pg.event_count(connection, schema)
    report["subjects"] = temporal_pg.subjects(connection, schema)
    return report


def trust_health(connection, schema: str,
                 project_dir: Path) -> dict[str, Any]:
    """Trust section for memory_health. Zero revocations is healthy;
    builtin-default policy is healthy; malformed policy is not."""
    from remembering.trust import postgres as standing_pg
    from remembering.trust.model import (
        INSTRUCTION_SCREEN_VERSION,
        TRUST_ENGINE_VERSION,
    )
    from remembering.trust.policy import load_trust_policy
    from remembering.trust.standing import resolve_standing

    section: dict[str, Any] = {
        "trust_engine_version": TRUST_ENGINE_VERSION,
        "configured": False,
        "policy_source": "builtin_default",
        "policy_version": None,
        "policy_digest": None,
        "policy_valid": True,
        "instruction_screen_version": INSTRUCTION_SCREEN_VERSION,
        "standing_store_ready": False,
        "standing_events": 0,
        "revoked_sources": [],
        "restricted_sources": {},
        "policy_error": None,
    }
    loaded = load_trust_policy(Path(os.path.realpath(project_dir)))
    section["configured"] = loaded["configured"]
    section["policy_valid"] = loaded["valid"]
    if not loaded["valid"]:
        section["policy_error"] = loaded["error"]
        return section
    policy = loaded["policy"]
    section["policy_source"] = policy.policy_source
    section["policy_version"] = policy.version
    section["policy_digest"] = policy.digest
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.standing_events",))
            if cur.fetchone()[0] is None:
                return section
        section["standing_store_ready"] = True
        section["standing_events"] = standing_pg.event_count(
            connection, schema)
        standing = resolve_standing(
            standing_pg.load_events(connection, schema))
        section["revoked_sources"] = sorted(standing["revoked"])
        section["restricted_sources"] = standing["restricted"]
    except Exception as exc:
        section["policy_error"] = (
            f"{type(exc).__name__}: {str(exc)[:160]}")
        section["policy_valid"] = False
    return section


def trace_health(connection, schema: str) -> dict[str, Any]:
    """Trace section for memory_health. Zero traces is healthy."""
    from remembering.trace import postgres as trace_pg
    from remembering.trace.model import (
        REPLAY_PROTOCOL_VERSION,
        TRACE_ENGINE_VERSION,
        TRACE_SCHEMA_VERSION,
        TRACE_STORE_VERSION,
    )

    section: dict[str, Any] = {
        "trace_engine_version": TRACE_ENGINE_VERSION,
        "store_version": TRACE_STORE_VERSION,
        "schema_version": TRACE_SCHEMA_VERSION,
        "replay_version": REPLAY_PROTOCOL_VERSION,
        "ready": False,
        "trace_count": 0,
        "oldest_trace": None,
        "newest_trace": None,
        "retention": "indefinite",
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.context_traces",))
            if cur.fetchone()[0] is None:
                return section
        section["ready"] = True
        counts = trace_pg.trace_count(connection, schema)
        section["trace_count"] = counts["traces"]
        section["oldest_trace"] = counts["oldest"]
        section["newest_trace"] = counts["newest"]
        stored_versions = trace_pg.versions(connection, schema)
        if stored_versions.get("store_version"):
            section["store_version"] = stored_versions["store_version"]
        if stored_versions.get("schema_version"):
            section["schema_version"] = stored_versions["schema_version"]
    except Exception as exc:
        section["error"] = (f"{type(exc).__name__}: {str(exc)[:160]}")
    return section


def loops_health(connection, schema: str,
                 project_dir: Path) -> dict[str, Any]:
    """Loop section for memory_health. Zero loops is healthy."""
    from remembering.loops import postgres as loops_pg
    from remembering.loops.model import (
        LOOP_CLOSURE_VERSION,
        LOOP_ENGINE_VERSION,
        LOOP_EVENT_SCHEMA,
        LOOP_REDUCER_VERSION,
    )

    section: dict[str, Any] = {
        "loops_engine_version": LOOP_ENGINE_VERSION,
        "store_version": None,
        "event_schema_version": LOOP_EVENT_SCHEMA,
        "reducer_version": LOOP_REDUCER_VERSION,
        "closure_version": LOOP_CLOSURE_VERSION,
        "ready": False,
        "event_count": 0,
        "loop_count": 0,
        "open": 0,
        "completed": 0,
        "cancelled": 0,
        "superseded": 0,
        "uncertain": 0,
        "unresolved_evidence_refs": [],
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.open_loop_events",))
            if cur.fetchone()[0] is None:
                return section
        section["ready"] = True
        section["event_count"] = loops_pg.event_count(connection, schema)
        loaded = load_loop_views(connection, schema, project_dir)
        views = loaded["views"]
        section["loop_count"] = len(views)
        for view in views.values():
            if view.state.value in section:
                section[view.state.value] += 1
        versions = loaded["versions"]
        if versions.get("store_version"):
            section["store_version"] = versions["store_version"]
        refs: set[str] = set()
        for view in views.values():
            for ref in view.evidence_refs:
                refs.add(ref)
        with connection.cursor() as cur:
            from psycopg import sql as _sql

            try:
                cur.execute(_sql.SQL(
                    "SELECT source_id FROM {}.sources").format(
                        _sql.Identifier(schema)))
                known = {row[0] for row in cur.fetchall()}
            except Exception:
                known = set()
        section["unresolved_evidence_refs"] = sorted(
            ref for ref in refs
            if ref and ref not in known and not ref.startswith("loop:"))
    except Exception as exc:
        section["error"] = (f"{type(exc).__name__}: {str(exc)[:160]}")
    return section


def load_temporal_log(connection, schema: str):
    from remembering.temporal import postgres as temporal_pg

    ensure_temporal_objects(connection, schema)
    return temporal_pg.load_log(connection, schema)


def ensure_project_meta(store, canonical_dir: str) -> str:
    """Record project identity; fail closed on a catastrophic mismatch."""
    from psycopg import sql

    with store.conn.cursor() as cur:
        meta = read_project_meta(cur, store.schema)
        recorded = meta.get("project.path")
        if recorded is not None and recorded != canonical_dir:
            raise BridgeError(
                "SCHEMA_MISMATCH",
                f"schema {store.schema!r} is already claimed by project "
                f"{recorded!r}, but this project resolves to "
                f"{canonical_dir!r}. Refusing to mix projects: fix the "
                "schema configuration instead of sharing an index.",
            )
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for key, value in (("project.path", canonical_dir),
                           ("project.schema", store.schema),
                           ("project.recorded_at", stamp)):
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(sql.Identifier(store.schema)),
                (key, value),
            )
    return recorded or canonical_dir


# -- doctor -----------------------------------------------------------------

def doctor(payload: dict[str, Any]) -> dict[str, Any]:
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    emb = embedding_spec_from(payload)
    ret = retrieval_spec_from(payload)
    redacted = redact_dsn(dsn())

    report: dict[str, Any] = {
        "ok": False,
        "engine_root": "",
        "engine_version": "",
        "dsn_redacted": redacted,
        "schema": schema,
        "project_directory": canonical_dir,
        "postgres_reachable": False,
        "database_exists": False,
        "pgvector_available": False,
        "pgvector_version": None,
        "pg_trgm_available": False,
        "engine_available": False,
        "python_dependencies": {"psycopg": False},
        "schema_initialized": False,
        "schema_identity_ok": True,
        "embedding_provider_reachable": False,
        "embedding_model_available": False,
        "embedding_detail": "",
        "stored_embedding_version": None,
        "stored_embedding_dimension": None,
        "configured_embedding_version": f"{emb['provider']}:{emb['model']}",
        "embedding_dimension_compatible": None,
        "fts_index_present": False,
        "hnsw_index_present": False,
        "source_count": None,
        "chunk_count": None,
        # bootstrap aliases
        "pgvector_version": "unknown",
        "indexed": False,
        "chunks": None,
        # Temporal subsystem: structurally ready vs populated are
        # reported separately; zero events never fails the store.
        "temporal": {
            "store_ready": False,
            "store_version": None,
            "event_schema_version": None,
            "reducer_version": None,
            "events": 0,
            "subjects": [],
            "unknown_references": [],
            "causality_violations": [],
            "sequence_gaps": [],
        },
        # Framing is optional configuration: a project without a frame
        # is a healthy retrieval/temporal system with framing disabled.
        "frame": {
            "project_frame_present": False,
            "project_frame_valid": False,
            "project_frame_version": None,
            "project_frame_digest": None,
            "known_work_types": [],
            "framing_engine_version": "framing-engine-v0.1",
            "frame_error": None,
        },
        # Trust is permission, not truth. Missing policy uses the
        # conservative builtin (healthy); malformed policy is not.
        "trust": {
            "trust_engine_version": "trust-engine-v0.1",
            "configured": False,
            "policy_source": "builtin_default",
            "policy_version": None,
            "policy_digest": None,
            "policy_valid": True,
            "instruction_screen_version": "instruction-screen-v0.1",
            "standing_store_ready": False,
            "standing_events": 0,
            "revoked_sources": [],
            "restricted_sources": {},
            "policy_error": None,
        },
        "selection": {
            "select_engine_version": "select-engine-v0.1",
            "policy_version": "decisive-selection-v0.1",
            "budget_version": "context-budget-v0.1",
            "redundancy_version": "redundancy-v0.1",
            "provenance_version": "provenance-selection-v0.1",
        },
        "trace": {
            "trace_engine_version": "trace-engine-v0.1",
            "store_version": None,
            "schema_version": "context-trace-v0.1",
            "replay_version": "trace-replay-v0.1",
            "ready": False,
            "trace_count": 0,
            "oldest_trace": None,
            "newest_trace": None,
            "retention": "indefinite",
        },
        "loops": {
            "loops_engine_version": "loops-engine-v0.1",
            "store_version": None,
            "event_schema_version": "loop-event-v0.1",
            "reducer_version": "loop-reducer-v0.1",
            "closure_version": "loop-closure-v0.1",
            "ready": False,
            "event_count": 0,
            "loop_count": 0,
            "open": 0,
            "completed": 0,
            "cancelled": 0,
            "superseded": 0,
            "uncertain": 0,
            "unresolved_evidence_refs": [],
        },
        "writes": {
            "write_engine_version": "write-engine-v0.1",
            "store_version": None,
            "record_schema_version": "explicit-memory-record-v0.1",
            "action_schema_version": "memory-action-v0.1",
            "relation_version": "memory-relation-v0.1",
            "ready": False,
            "policy": {
                "configured": False,
                "valid": True,
                "version": None,
                "digest": None,
                "source": "builtin_default",
                "error": None,
            },
            "record_count": 0,
            "action_count": 0,
            "remember_count": 0,
            "correct_count": 0,
            "supersede_count": 0,
            "retract_count": 0,
            "relationship_count": 0,
            "unresolved_index_records": [],
            "last_action_at": None,
        },
    }

    def fail(message: str) -> dict[str, Any]:
        report["message"] = message
        return report

    try:
        engine_dir = require_engine()
    except BridgeError as exc:
        return fail(f"{exc.code}: {exc.message}")
    report["engine_root"] = str(engine_dir)
    try:
        from remembering import ENGINE_VERSION

        report["engine_version"] = ENGINE_VERSION
    except Exception as exc:
        return fail(f"ENGINE_MISSING: bundled engine failed to import: {exc}")

    try:
        import psycopg  # noqa: F401

        report["python_dependencies"] = {"psycopg": True}
    except Exception as exc:
        report["python_dependencies"] = {"psycopg": False}
        return fail(f"Python dependency psycopg is not importable: {exc}. "
                    "Install it with: python -m pip install "
                    "-r engine/requirements.txt")
    report["engine_available"] = True

    try:
        connection = raw_connect()
    except BridgeError as exc:
        if exc.code == "DB_MISSING":
            report["postgres_reachable"] = True
            return fail(f"{exc.code}: {exc.message}")
        return fail(f"{exc.code}: {exc.message}")

    try:
        with connection.cursor() as cur:
            report["postgres_reachable"] = True
            report["database_exists"] = True
            vector = extension_status(cur, "vector")
            trgm = extension_status(cur, "pg_trgm")
            report["pgvector_available"] = bool(vector["installed"])
            report["pgvector_version"] = vector.get("version")
            report["pg_trgm_available"] = bool(trgm["installed"])
            cur.execute("SELECT to_regclass(%s)", (f"{schema}.chunks",))
            initialized = cur.fetchone()[0] is not None
            report["schema_initialized"] = initialized
            report["indexed"] = initialized
            if vector.get("version"):
                report["pgvector_version"] = str(vector["version"])
            if initialized:
                from psycopg import sql

                meta = read_project_meta(cur, schema)
                recorded = meta.get("project.path")
                if recorded is not None and recorded != canonical_dir:
                    report["schema_identity_ok"] = False
                    return fail(
                        "SCHEMA_MISMATCH: schema "
                        f"{schema!r} is claimed by project {recorded!r}, "
                        f"not {canonical_dir!r}. Refusing to read another "
                        "project's memory."
                    )
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.sources").format(
                        sql.Identifier(schema))
                )
                report["source_count"] = int(cur.fetchone()[0])
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.chunks").format(
                        sql.Identifier(schema))
                )
                report["chunk_count"] = int(cur.fetchone()[0])
                report["chunks"] = report["chunk_count"]
                cur.execute(
                    sql.SQL(
                        "SELECT embedding_version, count(*) FROM {}.chunks "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
                    ).format(sql.Identifier(schema))
                )
                row = cur.fetchone()
                if row is not None:
                    report["stored_embedding_version"] = str(row[0])
                cur.execute(
                    sql.SQL(
                        "SELECT atttypmod FROM pg_attribute "
                        "JOIN pg_class ON pg_class.oid = "
                        "pg_attribute.attrelid "
                        "JOIN pg_namespace ON pg_namespace.oid = "
                        "pg_class.relnamespace "
                        "WHERE pg_namespace.nspname = %s "
                        "AND pg_class.relname = 'chunks' "
                        "AND pg_attribute.attname = 'embedding'"
                    ),
                    (schema,),
                )
                row = cur.fetchone()
                if row is not None and row[0] != -1:
                    report["stored_embedding_dimension"] = int(row[0])
                cur.execute(
                    sql.SQL(
                        "SELECT to_regclass('{}.chunks_tsv_idx'), "
                        "to_regclass('{}.chunks_embedding_hnsw')"
                    ).format(
                        sql.Identifier(schema), sql.Identifier(schema))
                )
                row = cur.fetchone()
                report["fts_index_present"] = row[0] is not None
                report["hnsw_index_present"] = (
                    len(row) > 1 and row[1] is not None)
                report["temporal"] = temporal_health(connection, schema)
                report["frame"] = frame_health(project_dir, schema)
                report["trust"] = trust_health(connection, schema,
                                               project_dir)
                report["trace"] = trace_health(connection, schema)
                report["loops"] = loops_health(connection, schema,
                                               project_dir)
                report["writes"] = write_health(connection, schema,
                                                project_dir)
    finally:
        connection.close()

    try:
        emb_check = check_embedding(emb)
        report["embedding_provider_reachable"] = True
        report["embedding_model_available"] = True
        report["embedding_detail"] = emb_check["detail"]
    except BridgeError as exc:
        report["embedding_detail"] = f"{exc.code}: {exc.message}"
        return fail(f"{exc.code}: {exc.message}")

    # Dimension compatibility: probe the real provider (one short call)
    # and compare against what the store holds. Never silently mix dims.
    if report["stored_embedding_dimension"] is not None:
        try:
            require_engine()
            embedder = build_embedder(emb)
            probe = embedder.embed(["dimension probe"])
            report["embedding_dimension_compatible"] = (
                probe.dimension == report["stored_embedding_dimension"]
            )
            if not report["embedding_dimension_compatible"]:
                return fail(
                    "DIMENSION_MISMATCH: store holds "
                    f"{report['stored_embedding_dimension']}-dimensional "
                    f"vectors but {emb['provider']}:{emb['model']} produces "
                    f"{probe.dimension}. Re-embed with a fresh schema "
                    "instead of mixing dimensions."
                )
        except BridgeError as exc:
            return fail(f"{exc.code}: {exc.message}")
        except Exception as exc:
            report["embedding_detail"] = (
                f"embedding probe failed: {type(exc).__name__}: "
                f"{str(exc)[:160]}")
            return fail(report["embedding_detail"])

    problems: list[str] = []
    if not report["pgvector_available"]:
        problems.append(
            "pgvector is not enabled in this database; run memory_setup.")
    if not report["pg_trgm_available"]:
        problems.append(
            "pg_trgm is not enabled in this database; run memory_setup.")
    if not report["schema_initialized"]:
        problems.append(
            "this project's remembering schema has not been indexed "
            "yet; run memory_setup.")
    if report["chunk_count"] == 0:
        problems.append("the project schema exists but holds no chunks.")
    if problems:
        return fail(" ".join(problems))

    report["ok"] = True
    report["message"] = (
        f"Remembering engine {report['engine_version']} is ready: schema "
        f"{schema} holds "
        f"{report['chunk_count']} chunks from {report['source_count']} "
        f"sources ({report['stored_embedding_version']})."
    )
    return report


# -- setup / refresh ----------------------------------------------------------

def _timed(steps: list[dict[str, Any]], name: str, func):
    started = time.perf_counter()
    try:
        detail = func()
        steps.append({"name": name, "ok": True,
                      "ms": round((time.perf_counter() - started) * 1000, 1),
                      "detail": detail})
    except BridgeError:
        steps.append({"name": name, "ok": False,
                      "ms": round((time.perf_counter() - started) * 1000, 1),
                      "detail": "failed"})
        raise
    except Exception as exc:
        steps.append({"name": name, "ok": False,
                      "ms": round((time.perf_counter() - started) * 1000, 1),
                      "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
        raise


def refresh_report_to_dict(report) -> dict[str, Any]:
    return {
        "ok": True,
        "discovered": report.discovered,
        "indexed": report.indexed,
        "unchanged": report.unchanged,
        "added": report.added,
        "changed": report.changed,
        "removed": report.removed,
        "chunks": report.chunks,
        "embedded": report.embedded,
        "failed": list(report.failed),
    }


def validate_trust_config(project_dir: Path) -> dict[str, Any]:
    """Validate explicit trust configuration if present. Absence
    yields the builtin default (healthy); malformed files fail."""
    from remembering.trust.policy import load_trust_policy

    loaded = load_trust_policy(Path(os.path.realpath(project_dir)))
    if not loaded["valid"]:
        return {"error": loaded["error"], "detail": "invalid"}
    if not loaded["configured"]:
        return {"error": None, "detail": "builtin default"}
    policy = loaded["policy"]
    return {"error": None,
            "detail": f"explicit v{policy.version}"}


def do_setup(payload: dict[str, Any]) -> dict[str, Any]:
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    emb = embedding_spec_from(payload)
    ret = retrieval_spec_from(payload)
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": False,
        "schema": schema,
        "project_directory": canonical_dir,
        "steps": steps,
        "embedding": {},
        "refresh": None,
    }

    def fail(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, BridgeError):
            result["message"] = f"{exc.code}: {exc.message}"
        else:
            result["message"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return result

    root = Path()
    try:
        def s_checkout():
            nonlocal root
            root = require_engine()
            from remembering import ENGINE_VERSION  # noqa: F401
            from remembering.baseline import (  # noqa: F401
                config,
                context,
                embeddings,
                health,
                ingest,
                pipeline,
                retrieval,
                routing,
                storage,
                temporal,
            )
            from remembering.temporal import (  # noqa: F401
                log,
                model,
                ordering,
                postgres,
                query,
                reducer,
            )
            import psycopg  # noqa: F401
            return f"bundled remembering engine {ENGINE_VERSION} at {root}"

        _timed(steps, "engine", s_checkout)

        state: dict[str, Any] = {}

        def s_postgres():
            connection = raw_connect()
            state["connection"] = connection
            return f"connected to {redact_dsn(dsn())}"

        _timed(steps, "postgres", s_postgres)

        def s_extensions():
            assert state.get("connection") is not None
            with state["connection"].cursor() as cur:
                vector = ensure_extension(cur, "vector")
                trgm = ensure_extension(cur, "pg_trgm")
            state["connection"].close()
            state["connection"] = None
            return (f"vector {vector.get('version')}, "
                    f"pg_trgm {trgm.get('version') or 'enabled'}")

        _timed(steps, "extensions", s_extensions)

        def s_embedding():
            check = check_embedding(emb)
            embedder = build_embedder(emb)
            probe = embedder.embed(["dimension probe"])
            state["embedder"] = embedder
            state["dimension"] = probe.dimension
            result["embedding"] = {
                "provider": probe.provider,
                "model": probe.model,
                "dimension": probe.dimension,
                "version": (f"{probe.provider}:{probe.model}:"
                            f"{probe.dimension}"),
            }
            return (f"{probe.provider}:{probe.model} "
                    f"dimension {probe.dimension} ({check['detail']})")

        _timed(steps, "embedding", s_embedding)

        def s_initialise():
            from remembering.baseline.pipeline import Baseline

            config = build_baseline_config(schema, emb, ret)
            baseline = Baseline(config, state["embedder"])
            try:
                baseline.initialise()
            except ValueError as exc:
                raise BridgeError("DIMENSION_MISMATCH", str(exc))
            ensure_project_meta(baseline.store, canonical_dir)
            temporal_versions = ensure_temporal_objects(
                baseline.store.conn, schema)
            from remembering.trust import postgres as standing_pg

            standing_versions = standing_pg.initialise(
                baseline.store.conn, schema)
            from remembering.trace import postgres as trace_pg

            trace_versions = trace_pg.initialise(
                baseline.store.conn, schema)
            from remembering.loops import postgres as loops_pg

            loop_versions = loops_pg.initialise(
                baseline.store.conn, schema)
            trust_check = validate_trust_config(project_dir)
            if trust_check["error"]:
                raise BridgeError("TRUST_POLICY_INVALID",
                                  trust_check["error"])
            from remembering.write import postgres as write_pg

            write_versions = write_pg.initialise(
                baseline.store.conn, schema)
            write_check = validate_write_config(project_dir)
            if write_check["error"]:
                raise BridgeError("WRITE_POLICY_INVALID",
                                  write_check["error"])
            state["baseline"] = baseline
            state["temporal_versions"] = temporal_versions
            return (f"schema {schema} initialised "
                    f"(embedding dim {state['dimension']}; "
                    f"{temporal_versions['store_version']}; "
                    f"{standing_versions['store_version']}; "
                    f"{trace_versions['store_version']}; "
                    f"{loop_versions['store_version']}; "
                    f"{write_versions['store_version']}; "
                    f"trust policy {trust_check['detail']}; "
                    f"write policy {write_check['detail']})")

        _timed(steps, "initialise", s_initialise)

        def s_refresh():
            baseline = state["baseline"]
            refresh = baseline.refresh(Path(os.path.realpath(project_dir)))
            temporal = import_temporal_events(
                baseline.store.conn, schema, project_dir)
            standing = import_standing_events(
                baseline.store.conn, schema, project_dir)
            loops = import_loop_events(
                baseline.store.conn, schema, project_dir)
            memory = import_memory_events(
                baseline.store.conn, schema, project_dir,
                state["embedder"])
            refresh_dict = refresh_report_to_dict(refresh)
            refresh_dict["schema"] = schema
            refresh_dict["temporal"] = temporal
            refresh_dict["trust"] = standing
            refresh_dict["loops"] = loops
            refresh_dict["writes"] = memory
            result["refresh"] = refresh_dict
            if refresh.unchanged and not (
                    refresh.added or refresh.changed or refresh.removed):
                summary = (f"nothing changed ({refresh.unchanged} "
                           "unchanged)")
            else:
                summary = (f"+{refresh.added} ~{refresh.changed} "
                           f"-{refresh.removed} ={refresh.embedded} "
                           "embedded")
            temporal_note = ""
            if temporal["failed"]:
                temporal_note = (f"; temporal import failures: "
                                 f"{temporal['failed']}")
            elif temporal["imported"]:
                temporal_note = (f"; temporal events +{temporal['imported']}")
            if standing["failed"]:
                temporal_note += (f"; standing import failures: "
                                  f"{standing['failed']}")
            elif standing["imported"]:
                temporal_note += (f"; standing events "
                                  f"+{standing['imported']}")
            if loops["failed"]:
                temporal_note += (f"; loop import failures: "
                                  f"{loops['failed']}")
            elif loops["imported"]:
                temporal_note += (f"; loop events +{loops['imported']}")
            if memory["failed"]:
                temporal_note += (f"; memory import failures: "
                                  f"{memory['failed']}")
            elif memory["imported"]:
                temporal_note += (f"; memory actions "
                                  f"+{memory['imported']}")
            if memory["denied"]:
                temporal_note += (f"; memory import denied: "
                                  f"{len(memory['denied'])}")
            return (f"discovered {refresh.discovered}, {summary}; "
                    f"failed: {refresh.failed or 'none'}{temporal_note}")

        _timed(steps, "refresh", s_refresh)

        def s_verify():
            baseline = state["baseline"]
            baseline.verify()
            stats = baseline.store.stats()
            baseline.close()
            return (f"{stats['sources']} sources, {stats['chunks']} "
                    f"chunks, fts={'on' if stats['fts_index'] else 'off'}, "
                    f"hnsw={'on' if stats['hnsw_index'] else 'off'}")

        _timed(steps, "verify", s_verify)
    except Exception as exc:
        try:
            conn = locals().get("state", {}).get("connection")
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            baseline = locals().get("state", {}).get("baseline")
            if baseline is not None:
                baseline.close()
        except Exception:
            pass
        return fail(exc)

    result["ok"] = True
    result["message"] = (
        f"schema {schema} is ready: "
        f"{result['refresh']['embedded']} chunks embedded from "
        f"{result['refresh']['indexed']} sources "
        f"({result['embedding']['version']}).")
    return result


def do_refresh(payload: dict[str, Any]) -> dict[str, Any]:
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    emb = embedding_spec_from(payload)
    ret = retrieval_spec_from(payload)
    root = require_engine()
    _ = root
    from remembering.baseline.pipeline import Baseline

    try:
        check_embedding(emb)
        embedder = build_embedder(emb)
        config = build_baseline_config(schema, emb, ret)
        baseline = Baseline(config, embedder)
        try:
            try:
                baseline.initialise()
            except ValueError as exc:
                raise BridgeError("DIMENSION_MISMATCH", str(exc))
            ensure_project_meta(baseline.store, canonical_dir)
            ensure_temporal_objects(baseline.store.conn, schema)
            from remembering.trust import postgres as standing_pg
            from remembering.trace import postgres as trace_pg
            from remembering.loops import postgres as loops_pg
            from remembering.write import postgres as write_pg

            standing_pg.initialise(baseline.store.conn, schema)
            trace_pg.initialise(baseline.store.conn, schema)
            loops_pg.initialise(baseline.store.conn, schema)
            write_pg.initialise(baseline.store.conn, schema)
            refresh = baseline.refresh(Path(os.path.realpath(project_dir)))
            temporal = import_temporal_events(
                baseline.store.conn, schema, project_dir)
            standing = import_standing_events(
                baseline.store.conn, schema, project_dir)
            loops = import_loop_events(
                baseline.store.conn, schema, project_dir)
            memory = import_memory_events(
                baseline.store.conn, schema, project_dir, embedder)
            out = refresh_report_to_dict(refresh)
            out["schema"] = schema
            out["temporal"] = temporal
            out["trust"] = standing
            out["loops"] = loops
            out["writes"] = memory
            if temporal["failed"]:
                out["message"] = (
                    f"discovered {refresh.discovered}: +{refresh.added} "
                    f"~{refresh.changed} -{refresh.removed}, "
                    f"{refresh.embedded} chunks embedded; "
                    f"TEMPORAL import failures: {temporal['failed']}")
            elif standing["failed"]:
                out["message"] = (
                    f"discovered {refresh.discovered}: +{refresh.added} "
                    f"~{refresh.changed} -{refresh.removed}, "
                    f"{refresh.embedded} chunks embedded; "
                    f"STANDING import failures: {standing['failed']}")
            elif loops["failed"]:
                out["message"] = (
                    f"discovered {refresh.discovered}: +{refresh.added} "
                    f"~{refresh.changed} -{refresh.removed}, "
                    f"{refresh.embedded} chunks embedded; "
                    f"LOOP import failures: {loops['failed']}")
            elif memory["failed"] or memory["denied"]:
                out["message"] = (
                    f"discovered {refresh.discovered}: +{refresh.added} "
                    f"~{refresh.changed} -{refresh.removed}, "
                    f"{refresh.embedded} chunks embedded; "
                    f"MEMORY import: +{memory['imported']} "
                    f"duplicates={memory['duplicates']} "
                    f"denied={len(memory['denied'])} "
                    f"failed={memory['failed']}")
            elif refresh.unchanged and not (
                    refresh.added or refresh.changed or refresh.removed):
                out["message"] = (f"nothing changed: {refresh.unchanged} "
                                  "sources unchanged, 0 embedded.")
            else:
                out["message"] = (
                    f"discovered {refresh.discovered}: +{refresh.added} "
                    f"~{refresh.changed} -{refresh.removed}, "
                    f"{refresh.embedded} chunks embedded.")
            return out
        finally:
            baseline.close()
    except BridgeError as exc:
        return {"ok": False, "schema": schema,
                "message": f"{exc.code}: {exc.message}",
                "discovered": 0, "indexed": 0, "unchanged": 0, "added": 0,
                "changed": 0, "removed": 0, "chunks": 0, "embedded": 0,
                "failed": []}


# -- hybrid retrieval -----------------------------------------------------------

def build_retriever(schema: str, emb: dict[str, Any], ret: dict[str, Any]):
    from remembering.baseline.config import RetrievalConfig
    from remembering.baseline.retrieval import Retriever
    from remembering.baseline.storage import Store

    store = Store(dsn(), schema)
    embedder = build_embedder(emb)
    cfg = RetrievalConfig(
        mode=ret["mode"], lexical_k=ret["lexical_k"],
        dense_k=ret["dense_k"], fusion_k=ret["fusion_k"],
        reranker=ret["reranker"], rerank_k=ret["rerank_k"],
    )
    return store, embedder, Retriever(store, embedder, cfg)


def scored_to_dict(item, lexical_ranks, dense_ranks, final_stage: str):
    return {
        "chunk_id": item.chunk_id,
        "source_id": item.source_id,
        "section": item.section,
        "rank": item.rank,
        "score": item.score,
        "text": item.text,
        "lexical_rank": lexical_ranks.get(item.chunk_id),
        "dense_rank": dense_ranks.get(item.chunk_id),
        "stage": final_stage,
    }


def trace_to_dict(trace, emb_identity: dict[str, Any],
                  ret: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": trace.query,
        "mode": ret["mode"],
        "lexical_count": len(trace.lexical),
        "dense_count": len(trace.dense),
        "fused_count": len(trace.fused),
        "reranked_count": len(trace.reranked),
        "lexical_ids": [c.chunk_id for c in trace.lexical],
        "dense_ids": [c.chunk_id for c in trace.dense],
        "fused_ids": [c.chunk_id for c in trace.fused],
        "reranked_ids": [c.chunk_id for c in trace.reranked],
        "latencies_ms": {k: round(v, 2)
                         for k, v in trace.latencies_ms.items()},
        "embedding": emb_identity,
        "retrieval": {
            "mode": ret["mode"],
            "lexical_k": ret["lexical_k"],
            "dense_k": ret["dense_k"],
            "fusion_k": ret["fusion_k"],
            "rerank_k": ret["rerank_k"],
            "reranker": ret["reranker"],
        },
        # Proof that dense retrieval actually ran: the dense stage
        # executed a real pgvector query (not merely "an embedding
        # column exists").
        "dense_executed": "dense" in trace.latencies_ms,
    }


def schema_initialized(schema: str) -> bool:
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{schema}.chunks",))
            return cur.fetchone()[0] is not None
    finally:
        connection.close()


NOT_INDEXED_MESSAGE = (
    "This project's remembering schema has not been indexed yet. "
    "Run memory_setup to initialise the schema and ingest the repository."
)


def do_search(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    schema = schema_from(payload)
    emb = embedding_spec_from(payload)
    ret = retrieval_spec_from(payload)
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": True, "indexed": True, "schema": schema, "items": [],
                "trace": None,
                "message": "empty query: no retrieval attempted."}
    query = query.strip()
    limit = payload.get("limit", 8)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise BridgeError("CONFIG_INVALID", "limit must be an integer")
    limit = max(1, min(limit, 20))

    if not schema_initialized(schema):
        return {"ok": True, "indexed": False, "schema": schema, "items": [],
                "trace": None, "message": NOT_INDEXED_MESSAGE}

    check_embedding(emb)
    store, embedder, retriever = build_retriever(schema, emb, ret)
    try:
        # One short probe up front: proves the configured model answers
        # and pins the embedding identity recorded in the trace.
        probe = embedder.embed(["dimension probe"])
        emb_identity = {"provider": probe.provider, "model": probe.model,
                        "dimension": probe.dimension,
                        "version": (f"{probe.provider}:{probe.model}:"
                                    f"{probe.dimension}")}
        trace = retriever.retrieve(query)
        lexical_ranks = {c.chunk_id: c.rank for c in trace.lexical}
        dense_ranks = {c.chunk_id: c.rank for c in trace.dense}
        final_stage = ("reranked" if ret["reranker"] != "none"
                       else "fused")
        items = [scored_to_dict(c, lexical_ranks, dense_ranks, final_stage)
                 for c in trace.reranked[:limit]]
        annotate_search_standing(
            items, schema, project_dir_from_search(payload))
        annotate_explicit_metadata(items, schema)
        return {"ok": True, "indexed": True, "schema": schema,
                "items": items,
                "trace": trace_to_dict(trace, emb_identity, ret)}
    finally:
        store.close()


def project_dir_from_search(payload: dict[str, Any]) -> Path:
    try:
        return project_directory_from(payload)
    except BridgeError:
        return Path(".")


def annotate_search_standing(items: list[dict[str, Any]], schema: str,
                             project_dir: Path) -> None:
    """Search stays broad: standing annotations are added, nothing is
    hidden. Failures degrade to unannotated items, never to a failed
    search."""
    try:
        from remembering.trust import postgres as standing_pg
        from remembering.trust.policy import load_trust_policy, match_rule
        from remembering.trust.standing import resolve_standing

        loaded = load_trust_policy(Path(os.path.realpath(project_dir)))
        policy = loaded["policy"]
        connection = raw_connect()
        try:
            standing_pg.initialise(connection, schema)
            standing = resolve_standing(
                standing_pg.load_events(connection, schema))
        finally:
            connection.close()
        for item in items:
            rule, rule_id = match_rule(policy, item["source_id"])
            item["standing"] = {
                "source_class": (rule.source_class if rule is not None
                                 else policy.default_source_class),
                "role": (rule.role if rule is not None else "ordinary"),
                "matched_rule": rule_id,
                "revoked": item["source_id"] in standing["revoked"],
                "policy_version": policy.version,
            }
    except Exception:
        pass


# -- safe framing ------------------------------------------------------------
#
# Framing applies to INFLUENCE only, after temporal interpretation.
# Recall bypasses frame control (isolation aside). Establishment
# strength decides control: declared/corroborated -> HARD,
# inferred -> SOFT (assist, never erase), conflicting/stale/unknown ->
# QUERY_ONLY (Stage 3 path untouched).

def parse_work_spec(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("work") or {}
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID", "work must be an object")
    mode = raw.get("mode", "auto")
    if mode is None:
        mode = "auto"
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "auto", "explicit", "none"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown work mode {raw.get('mode')!r}: expected 'auto', "
            "'explicit' or 'none'.")
    mode = mode.strip().lower()
    explicit = None
    if mode == "explicit":
        work_type = raw.get("work_type")
        objective = raw.get("objective", "")
        if not isinstance(work_type, str) or not work_type.strip():
            raise BridgeError("CONFIG_INVALID",
                              "explicit work needs work_type")
        if objective is not None and not isinstance(objective, str):
            raise BridgeError("CONFIG_INVALID",
                              "explicit objective must be a string")
        explicit = {"work_type": work_type.strip(),
                    "objective": (objective or "").strip()}
    prior = raw.get("prior_work_type")
    if prior is not None and (not isinstance(prior, str) or not prior.strip()):
        raise BridgeError("CONFIG_INVALID",
                          "prior_work_type must be a non-empty string")
    signals = raw.get("signals", [])
    if not isinstance(signals, list):
        raise BridgeError("CONFIG_INVALID", "work.signals must be a list")
    return {"mode": mode, "explicit": explicit,
            "prior_work_type": prior.strip() if isinstance(prior, str)
            else None,
            "signals": signals}


def build_signals(raw_signals: list) -> list:
    from remembering.frame.signals import make_signal

    signals = []
    for entry in raw_signals:
        if not isinstance(entry, dict):
            raise BridgeError("CONFIG_INVALID",
                              "each work signal must be an object")
        for field in ("signal_id", "kind", "text", "observed_at"):
            if not isinstance(entry.get(field), str) \
                    or not entry[field].strip():
                raise BridgeError(
                    "CONFIG_INVALID",
                    f"work signal needs non-empty {field}")
        try:
            signals.append(make_signal(
                entry["signal_id"].strip(), entry["kind"].strip(),
                entry["text"], entry["observed_at"].strip()))
        except ValueError as exc:
            raise BridgeError("CONFIG_INVALID", str(exc))
    return signals


def frame_health(project_dir: Path, schema: str) -> dict[str, Any]:
    """Frame section for memory_health. Missing/invalid frames never
    fail the store; identity mismatch is reported, not raised, here
    (use paths raise fail-closed)."""
    from remembering.frame.model import FRAMING_ENGINE_VERSION
    from remembering.frame.project import FrameError, load_project_frame

    section: dict[str, Any] = {
        "project_frame_present": False,
        "project_frame_valid": False,
        "project_frame_version": None,
        "project_frame_digest": None,
        "known_work_types": [],
        "framing_engine_version": FRAMING_ENGINE_VERSION,
        "frame_error": None,
    }
    try:
        loaded = load_project_frame(Path(os.path.realpath(project_dir)),
                                    schema)
    except FrameError as exc:
        section["frame_error"] = f"{exc.code}:{exc.message}"
        return section
    section["project_frame_present"] = loaded["present"]
    section["project_frame_valid"] = loaded["valid"]
    if loaded["valid"] and loaded["frame"] is not None:
        section["project_frame_version"] = loaded["frame"].version
        section["project_frame_digest"] = loaded["digest"]
        section["known_work_types"] = list(
            loaded["frame"].work_type_names())
    elif loaded["error"]:
        section["frame_error"] = loaded["error"]
    return section


def frame_context_for(schema: str, project_dir: Path,
                      canonical_dir: str, work_spec: dict,
                      now: str) -> dict[str, Any]:
    """Establish frame context for an influence request.

    Returns a frame block with applied/control/work_frame/versions, or
    applied=False with a fallback reason. Project-identity mismatch
    raises (fail closed); malformed frames disable framing visibly.
    """
    from remembering.frame.establish import establish, work_frame_id
    from remembering.frame.model import (
        ESTABLISHMENT_POLICY_VERSION,
        FRAME_CONTROL_POLICY_VERSION,
        FRAMING_ENGINE_VERSION,
        Establishment,
        WorkFrame,
    )
    from remembering.frame.policy import (
        ESTABLISHMENT_REASONS,
        control_for,
        control_reason,
    )
    from remembering.frame.project import FrameError, load_project_frame

    versions = {
        "framing_engine_version": FRAMING_ENGINE_VERSION,
        "establishment_policy": ESTABLISHMENT_POLICY_VERSION,
        "control_policy": FRAME_CONTROL_POLICY_VERSION,
    }
    try:
        loaded = load_project_frame(Path(os.path.realpath(project_dir)),
                                    schema)
    except FrameError as exc:
        if exc.code == "FRAME_PROJECT_MISMATCH":
            raise BridgeError("FRAME_PROJECT_MISMATCH", exc.message)
        return {"applied": False, "reason": "frame.load_failed",
                "detail": f"{exc.code}:{exc.message}", **versions}
    if not loaded["present"]:
        return {"applied": False, "reason": "frame.no_project_frame",
                **versions}
    if not loaded["valid"]:
        return {"applied": False, "reason": "frame.invalid_project_frame",
                "detail": loaded["error"], **versions}
    project_frame = loaded["frame"]
    try:
        signals = build_signals(work_spec["signals"])
    except BridgeError as exc:
        return {"applied": False, "reason": "frame.bad_signals",
                "detail": f"{exc.code}:{exc.message}", **versions}
    try:
        result = establish(signals, project_frame,
                           explicit=work_spec["explicit"],
                           prior_work_type=work_spec["prior_work_type"])
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
    control = control_for(result.establishment)
    work_frame = None
    if result.work_type is not None:
        provenance = (
            ("objective", tuple(result.supporting_refs)),
            ("work_type", tuple(result.supporting_refs)),
        )
        work_frame = WorkFrame(
            work_frame_id=work_frame_id(
                schema, result.work_type, tuple(result.supporting_refs),
                now),
            project_id=schema,
            objective=result.objective,
            work_type=result.work_type,
            as_of=now,
            active_constraints=tuple(project_frame.constraints),
            signals=tuple(signals),
            provenance=provenance,
            establishment=result.establishment,
        )
    return {
        "applied": True,
        "reason": ESTABLISHMENT_REASONS[result.establishment],
        "control": control.value,
        "control_reason": control_reason(control, result.establishment),
        "establishment": result.to_dict(),
        "work_frame": None if work_frame is None else {
            "work_frame_id": work_frame.work_frame_id,
            "project_id": work_frame.project_id,
            "objective": work_frame.objective,
            "work_type": work_frame.work_type,
            "as_of": work_frame.as_of,
            "active_constraints": list(work_frame.active_constraints),
            "signal_ids": [s.signal_id for s in work_frame.signals],
            "provenance": {k: list(v)
                           for k, v in work_frame.provenance},
            "establishment": work_frame.establishment.value,
            "schema_version": work_frame.schema_version,
        },
        "project_frame_version": project_frame.version,
        "project_frame_digest": loaded["digest"],
        "project_constraints": list(project_frame.constraints),
        "preferred_classes": list(project_frame.preferences_for(
            result.work_type)) if result.work_type else [],
        "hard_exclude": project_frame.hard_exclude,
        "include_projects": list(project_frame.include_projects),
        **versions,
    }


def ret_fusion_k(payload: dict[str, Any]) -> int:
    raw = payload.get("retrieval") or {}
    value = raw.get("fusion_k", 60) if isinstance(raw, dict) else 60
    return value if isinstance(value, int) and not isinstance(
        value, bool) else 60


# -- trust / standing ---------------------------------------------------------
#
# The trust gate runs after framing on INFLUENCE: relevant, current,
# correctly framed evidence still needs standing to steer behaviour.
# RECALL annotates verdicts without excluding. Trust is permission,
# never truth: denied sources stay searchable history.

TRUST_CLAIMS_REL = Path(".remembering") / "trust" / "claims.json"
TRUST_EVENTS_REL = Path(".remembering") / "trust" / "events.jsonl"


def parse_trust_spec(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("trust") or {}
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID", "trust must be an object")
    caller = raw.get("caller_scope", "default")
    if caller is None:
        caller = "default"
    if not isinstance(caller, str) or not caller.strip():
        raise BridgeError("CONFIG_INVALID",
                          "trust.caller_scope must be a non-empty string")
    level = raw.get("level", "FULL")
    if level is None:
        level = "FULL"
    if not isinstance(level, str) or level.strip().upper() not in (
            "T0", "S1", "S2", "S3", "FULL"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown trust level {raw.get('level')!r}: expected one of "
            "T0, S1, S2, S3, FULL.")
    return {"caller_scope": caller.strip(), "level": level.strip().upper()}


def load_claims_map(project_dir: Path) -> dict[str, dict]:
    """Optional per-source claim/lineage metadata. Malformed explicit
    files fail visibly; absence means no structured lineage."""
    path = Path(os.path.realpath(project_dir)) / TRUST_CLAIMS_REL
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError("CONFIG_INVALID",
                          f"TRUST_CLAIMS_INVALID:unreadable: {exc}")
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID",
                          "TRUST_CLAIMS_INVALID:claims file must be an object")
    out: dict[str, dict] = {}
    for source_id, spec in raw.items():
        if not isinstance(source_id, str) or not source_id.strip():
            raise BridgeError("CONFIG_INVALID",
                              "TRUST_CLAIMS_INVALID:source ids must be strings")
        if not isinstance(spec, dict):
            raise BridgeError(
                "CONFIG_INVALID",
                f"TRUST_CLAIMS_INVALID:{source_id!r} must be an object")
        claim = {"claim_key": "", "derived_from": [], "refuted_by": [],
                 "role": "", "negative": False, "dispute": ""}
        for field in ("claim_key", "role", "dispute"):
            value = spec.get(field, "")
            if value is not None and not isinstance(value, str):
                raise BridgeError(
                    "CONFIG_INVALID",
                    f"TRUST_CLAIMS_INVALID:{source_id!r}.{field} "
                    "must be a string")
            claim[field] = (value or "").strip()
        for field in ("derived_from", "refuted_by"):
            value = spec.get(field, [])
            if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value):
                raise BridgeError(
                    "CONFIG_INVALID",
                    f"TRUST_CLAIMS_INVALID:{source_id!r}.{field} "
                    "must be a list of strings")
            claim[field] = [v.strip() for v in value if v.strip()]
        negative = spec.get("negative", False)
        if not isinstance(negative, bool):
            raise BridgeError(
                "CONFIG_INVALID",
                f"TRUST_CLAIMS_INVALID:{source_id!r}.negative "
                "must be a boolean")
        claim["negative"] = negative
        out[source_id.strip()] = claim
    return out


def import_standing_events(connection, schema: str,
                           project_dir: Path) -> dict[str, Any]:
    from remembering.trust import postgres as standing_pg
    from remembering.trust.model import StandingEvent

    report: dict[str, Any] = {"imported": 0, "duplicates": 0,
                              "failed": [], "events": 0}
    path = Path(os.path.realpath(project_dir)) / TRUST_EVENTS_REL
    if not path.is_file():
        return report
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report["failed"].append(f"{path}: unreadable: {exc}")
        return report
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            report["failed"].append(f"line {lineno}: not JSON: {exc}")
            continue
        received = raw.get("received_at") if isinstance(raw, dict) else None
        if not isinstance(received, str) or not received.strip():
            received = datetime.datetime.now(
                datetime.timezone.utc).isoformat()
        try:
            event = StandingEvent.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            report["failed"].append(
                f"line {lineno}: TRUST_EVENT_INVALID:malformed: {exc}")
            continue
        try:
            out = standing_pg.append_event(connection, schema, event,
                                           received)
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            report["failed"].append(
                f"line {lineno}: {code}:{event.event_id}")
            continue
        if out.get("duplicate"):
            report["duplicates"] += 1
        else:
            report["imported"] += 1
    report["events"] = standing_pg.event_count(connection, schema)
    return report


LOOP_EVENTS_REL = Path(".remembering") / "loops" / "events.jsonl"


def import_loop_events(connection, schema: str,
                       project_dir: Path) -> dict[str, Any]:
    from remembering.loops import postgres as loops_pg
    from remembering.loops.model import OpenLoopEvent

    report: dict[str, Any] = {"imported": 0, "duplicates": 0,
                              "failed": [], "events": 0, "loops": []}
    path = Path(os.path.realpath(project_dir)) / LOOP_EVENTS_REL
    if not path.is_file():
        return report
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report["failed"].append(f"{path}: unreadable: {exc}")
        return report
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            report["failed"].append(f"line {lineno}: not JSON: {exc}")
            continue
        received = raw.get("received_at") if isinstance(raw, dict) else None
        if not isinstance(received, str) or not received.strip():
            received = datetime.datetime.now(
                datetime.timezone.utc).isoformat()
        try:
            event = OpenLoopEvent.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            report["failed"].append(
                f"line {lineno}: LOOP_EVENT_INVALID:malformed: {exc}")
            continue
        try:
            out = loops_pg.append_event(connection, schema, event,
                                        received)
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            report["failed"].append(
                f"line {lineno}: {code}:{event.event_id}")
            continue
        if out.get("duplicate"):
            report["duplicates"] += 1
        else:
            report["imported"] += 1
    report["events"] = loops_pg.event_count(connection, schema)
    return report


def load_loop_views(connection, schema: str, project_dir: Path,
                    valid_at: str | None = None,
                    known_at: str | None = None) -> dict[str, Any]:
    """Project current loop states with temporal closure evidence."""
    from remembering.baseline.storage import Store
    from remembering.loops import postgres as loops_pg
    from remembering.loops.evidence import make_resolver
    from remembering.loops.reducer import project
    from remembering.temporal import postgres as temporal_pg
    from remembering.temporal.query import TemporalEngine

    loops_pg.initialise(connection, schema)
    events = loops_pg.load_events(connection, schema)
    from psycopg import sql as _sql

    try:
        with connection.cursor() as cur:
            cur.execute(_sql.SQL("SELECT source_id FROM {}.sources").format(
                _sql.Identifier(schema)))
            known = {row[0] for row in cur.fetchall()}
    except Exception:
        known = set()
    try:
        with connection.cursor() as cur:
            cur.execute(_sql.SQL(
                "SELECT source_id, text FROM {}.chunks").format(
                    _sql.Identifier(schema)))
            texts: dict[str, list[str]] = {}
            for source_id, text in cur.fetchall():
                texts.setdefault(source_id, []).append(text or "")
            source_texts = {source: "\n".join(parts)
                            for source, parts in texts.items()}
    except Exception:
        source_texts = {}
    temporal_log = temporal_pg.load_log(connection, schema)
    from remembering.temporal.query import TemporalEngine

    engine = TemporalEngine(temporal_log)
    resolve = make_resolver(temporal=engine, known_sources=known,
                            loop_events=events,
                            source_texts=source_texts)
    views = project(events, resolve, valid_at=valid_at, known_at=known_at)
    return {"views": views, "events": events,
            "versions": loops_pg.versions(connection, schema)}


def apply_trust_gate(schema: str, project_dir: Path,
                     ranked: list, by_annotation: dict,
                     frame_control: str, route_value: str,
                     trust_spec: dict, enforce: bool,
                     loop_meta: dict | None = None) -> dict[str, Any]:
    """Judge candidates; enforce only on influence. Returns the trust
    block (records, verdict ids, versions, latency). Nothing is hidden
    from the trace either way."""
    import time as _time

    from remembering.trust.gate import TrustContext, judge_all
    from remembering.trust.model import (
        INSTRUCTION_SCREEN_VERSION,
        TRUST_ENGINE_VERSION,
        TRUST_POLICY_VERSION,
        TrustCandidate,
    )
    from remembering.trust.policy import builtin_policy, load_trust_policy
    from remembering.trust import postgres as standing_pg
    from remembering.trust.standing import resolve_standing

    started = _time.perf_counter()
    loaded = load_trust_policy(Path(os.path.realpath(project_dir)))
    policy = loaded["policy"]
    claims = load_claims_map(project_dir)
    explicit_attrs: dict[str, dict] = {}
    try:
        from remembering.write import postgres as write_pg

        _wconn = raw_connect()
        try:
            explicit_attrs = write_pg.source_attributes(
                _wconn, schema,
                [c.source_id for c in ranked])
        finally:
            _wconn.close()
    except Exception:
        explicit_attrs = {}
    connection = raw_connect()
    try:
        standing_pg.initialise(connection, schema)
        events = standing_pg.load_events(connection, schema)
    finally:
        connection.close()
    standing = resolve_standing(events)

    from remembering.trust.policy import match_rule
    from remembering.write.model import effective_class

    candidates: list[TrustCandidate] = []
    roles_by_id: dict[str, str] = {}
    restricted_by_id: dict[str, list] = {}
    ceiling_by_id: dict[str, dict] = {}
    override: dict[str, str] = {}
    ceilings: dict[str, str] = {}
    loop_meta = loop_meta or {}
    for chunk in ranked:
        annotation = by_annotation.get(chunk.chunk_id)
        rule, rule_id = match_rule(policy, chunk.source_id)
        source_class = (rule.source_class if rule is not None
                        else policy.default_source_class)
        role = None
        claim = claims.get(chunk.source_id, {})
        loop_claim = loop_meta.get(chunk.chunk_id, {})
        explicit = explicit_attrs.get(chunk.source_id)
        if loop_claim.get("role"):
            role = loop_claim["role"]
        elif claim.get("role"):
            role = claim["role"]
        elif rule is not None:
            role = rule.role
        elif explicit is not None:
            # The write gate already authorized this role; the
            # trust policy classifies the source, never the caller.
            role = explicit.get("role") or "ordinary"
        else:
            role = "ordinary"
        roles_by_id[chunk.chunk_id] = role or "ordinary"
        restricted_by_id[chunk.chunk_id] = list(
            rule.restricted_to if rule is not None else [])
        restricted = list((rule.restricted_to if rule is not None else ()))
        derived = (loop_claim.get("derived_from")
                   if loop_claim.get("derived_from") is not None
                   else claim.get("derived_from", []))
        candidates.append(TrustCandidate(
            unit_id=chunk.chunk_id, source_id=chunk.source_id,
            project_id=schema, text=chunk.text,
            source_class=source_class, role=role or "ordinary",
            temporal_status=(annotation.status if annotation
                             else "not_modelled"),
            frame_control=frame_control,
            claim_key=loop_claim.get("claim_key",
                                     claim.get("claim_key", "")),
            derived_from=tuple(derived),
            refuted_by=tuple(claim.get("refuted_by", [])),
            restricted_to=tuple(restricted)))
        if explicit is not None and explicit.get("standing_ceiling"):
            # Two-key rule: the write ceiling caps the trust
            # resolution. Neither side escalates the other.
            ceiling = explicit["standing_ceiling"]
            policy_class = source_class
            effective = effective_class(policy_class, ceiling)
            override[chunk.source_id] = effective
            ceilings[chunk.source_id] = ceiling
            ceiling_by_id[chunk.chunk_id] = {
                "trust_policy_class": policy_class,
                "write_ceiling": ceiling,
                "effective_class": effective}
    derived_map = {c.source_id: list(c.derived_from) for c in candidates}
    known = ({c.source_id for c in candidates}
             | set(standing_pg_known_subjects(events)))
    ctx = TrustContext(
        policy=policy, revoked=standing["revoked"],
        restricted=standing["restricted"], derived_from=derived_map,
        known_sources=known, project_id=schema,
        caller_scope=trust_spec["caller_scope"],
        level=trust_spec["level"], standing_override=override,
        write_ceiling=ceilings)
    records = judge_all(candidates, ctx)
    latency_ms = round((_time.perf_counter() - started) * 1000, 2)
    admitted = [r.unit_id for r in records if r.verdict.value == "admit"]
    denied = [r.unit_id for r in records if r.verdict.value == "deny"]
    quarantined = [r.unit_id for r in records
                   if r.verdict.value == "quarantine"]
    classes = {}
    for c in candidates:
        classes[c.unit_id] = override.get(c.source_id,
                                          c.source_class)
    return {
        "block": {
            "mode": "enforce" if enforce else "annotate",
            "level": trust_spec["level"],
            "policy_version": policy.version,
            "policy_source": policy.policy_source,
            "policy_digest": policy.digest,
            "trust_policy_version": TRUST_POLICY_VERSION,
            "instruction_screen_version": INSTRUCTION_SCREEN_VERSION,
            "trust_engine_version": TRUST_ENGINE_VERSION,
            "caller_scope": trust_spec["caller_scope"],
            "revoked_sources": sorted(standing["revoked"]),
            "restricted_sources": standing["restricted"],
            "standing_events_applied": standing["applied"],
            "write_ceiling_by_id": ceiling_by_id,
            "records": [r.to_dict() for r in records],
            "admitted_ids": admitted,
            "denied_ids": denied,
            "quarantined_ids": quarantined,
            "latency_ms": latency_ms,
        },
        "records_by_id": {r.unit_id: r for r in records},
        "classes_by_id": classes,
        "roles_by_id": roles_by_id,
        "restricted_by_id": restricted_by_id,
        "ceiling_by_id": ceiling_by_id,
        "claims_map": claims,
        "admitted_ids": set(admitted),
    }


def standing_pg_known_subjects(events) -> set[str]:
    return {e.subject_id for e in events}


# -- decisive selection ------------------------------------------------------
#
# Admission (Stage 5) is permission; selection is necessity. Only
# ADMIT candidates enter influence selection; recall preserves
# broadly. No truth claims, no scalar scores, no consensus merging.

def parse_selection_spec(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("selection") or {}
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_INVALID", "selection must be an object")
    mode = raw.get("mode", "decisive")
    if mode is None:
        mode = "decisive"
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "decisive", "full"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown selection mode {raw.get('mode')!r}: expected "
            "'decisive' or 'full'.")
    return {"mode": mode.strip().lower()}


def constraint_hit(text: str, constraints: list) -> bool:
    lowered = (text or "").lower()
    for constraint in constraints:
        if not isinstance(constraint, str):
            continue
        for token in constraint.lower().split():
            if len(token) > 4 and token in lowered:
                return True
    return False


def apply_selection(schema: str, project_dir: Path,
                    ranked_selected: list, pool_items: list,
                    by_annotation: dict, trust_records: dict,
                    trust_classes: dict, trust_roles: dict,
                    framed_by_id: dict, frame_control: str,
                    route_value: str, selection_spec: dict,
                    enforce_trust: bool, constraints: list,
                    claims_map: dict, max_chars: int,
                    max_results: int,
                    retrieval_paths: dict) -> dict[str, Any]:
    """Run decisive selection over admitted candidates. Returns
    ordered chunks, records, and the trace block."""
    import time as _time

    from remembering.select.model import (
        BUDGET_POLICY_VERSION,
        PROVENANCE_POLICY_VERSION,
        REDUNDANCY_POLICY_VERSION,
        SELECT_ENGINE_VERSION,
        SELECTION_POLICY_VERSION,
        SelectionCandidate,
    )
    from remembering.select.policy import select

    started = _time.perf_counter()
    pool_by_id = {c["chunk_id"]: c for c in pool_items}
    query_ids = set((retrieval_paths.get("query") or {}).get("ids", []))
    objective_ids = set(
        ((retrieval_paths.get("objective") or {}) or {}).get("ids", []))
    derived_map = {source: list(spec.get("derived_from", []))
                   for source, spec in claims_map.items()}

    selection_candidates: list[SelectionCandidate] = []
    for chunk in ranked_selected:
        item = pool_by_id.get(chunk.chunk_id, {})
        annotation = by_annotation.get(chunk.chunk_id)
        record = trust_records.get(chunk.chunk_id)
        if enforce_trust and (
                record is None or record.to_dict()["verdict"] != "admit"):
            raise BridgeError(
                "SELECT_PIPELINE_INVARIANT",
                f"non-ADMIT candidate {chunk.chunk_id} entered selection")
        framed = framed_by_id.get(chunk.chunk_id)
        claim = claims_map.get(chunk.source_id, {})
        is_loop_pull = item.get("stage") == "loop-pull"
        selection_candidates.append(SelectionCandidate(
            chunk_id=chunk.chunk_id, source_id=chunk.source_id,
            text=item.get("text", chunk.text),
            fused_rank=item.get("rank", chunk.rank),
            lexical_rank=item.get("lexical_rank"),
            dense_rank=item.get("dense_rank"),
            score=float(item.get("score", chunk.score) or 0.0),
            from_query=chunk.chunk_id in query_ids,
            from_objective=chunk.chunk_id in objective_ids,
            temporal_status=(annotation.status if annotation
                             else "not_modelled"),
            frame_control=frame_control,
            evidence_class=(framed.evidence_class if framed is not None
                            else "prose"),
            frame_preferred=bool(framed and framed.preferred),
            trust_reason=(record.to_dict()["reason"] if record else ""),
            source_class=trust_classes.get(chunk.chunk_id,
                                           "informational"),
            role=trust_roles.get(chunk.chunk_id, "ordinary"),
            claim_key=item.get("loop_claim_key") or claim.get(
                "claim_key", ""),
            derived_from=tuple(item.get("loop_derived_from")
                               if item.get("loop_derived_from") is not None
                               else claim.get("derived_from", [])),
            dispute_key=claim.get("dispute", ""),
            negative=bool(claim.get("negative", False)),
            echo_exempt=bool(item.get("stage") == "loop-pull"),
            constraint_hit=constraint_hit(
                item.get("text", chunk.text), constraints)))

    if route_value == "recall":
        mode = "preserving"
    elif selection_spec["mode"] == "full":
        mode = "full"
    else:
        mode = "decisive"
    try:
        result = select(selection_candidates, derived_map,
                        max_chars=max_chars, max_results=max_results,
                        mode=mode)
    except ValueError as exc:
        raise BridgeError("SELECT_PROVENANCE_INVARIANT", str(exc))
    latency_ms = round((_time.perf_counter() - started) * 1000, 2)
    records_by_id = {r.chunk_id: r for r in
                     (*result.selected, *result.dropped)}
    ordered = [next(c for c in selection_candidates
                    if c.chunk_id == r.chunk_id)
               for r in result.selected]
    chars_before = sum(len(c.text) for c in selection_candidates)
    chars_after = sum(len(c.text) for c in ordered)
    block = {
        "mode": ("preserving" if route_value == "recall"
                 else selection_spec["mode"]),
        "policy_version": SELECTION_POLICY_VERSION,
        "budget_version": BUDGET_POLICY_VERSION,
        "redundancy_version": REDUNDANCY_POLICY_VERSION,
        "provenance_version": PROVENANCE_POLICY_VERSION,
        "select_engine_version": SELECT_ENGINE_VERSION,
        "input_count": len(selection_candidates),
        "selected_count": len(ordered),
        "dropped_redundant": sum(
            1 for r in result.dropped
            if r.disposition.value == "drop_redundant"),
        "dropped_low_value": sum(
            1 for r in result.dropped
            if r.disposition.value == "drop_low_value"),
        "dropped_budget": sum(
            1 for r in result.dropped
            if r.disposition.value == "drop_budget"),
        "chars_before": chars_before,
        "chars_after": chars_after,
        "compression_ratio": (round(chars_after / chars_before, 3)
                              if chars_before else 1.0),
        "budget_insufficient": result.budget_insufficient,
        "groups": result.groups,
        "records": [r.to_dict() for r in (*result.selected,
                                          *result.dropped)],
        "selected_ids": [r.chunk_id for r in result.selected],
        "latency_ms": latency_ms,
    }
    return {"ordered": ordered, "records_by_id": records_by_id,
            "block": block}


# -- durable context traces -----------------------------------------------------
#
# Stage 7 observes the pipeline; it makes no policy. Every successful
# context construction builds an immutable content-addressed
# ContextTrace, persists it before the bundle is returned, and hands
# the caller its trace_id. Influence requires persistence to succeed;
# recall degrades to an unpersisted trace rather than failing.

def build_durable_trace(schema: str, canonical_dir: str, now: str,
                        payload: dict, route_block: dict,
                        standpoint_echo: dict, work_spec: dict,
                        trust_spec: dict, selection_spec: dict,
                        retrieval_trace: dict, temporal_block: dict,
                        frame_trace: dict, trust_block: dict,
                        selection_block: dict, loop_block: dict,
                        lifecycles: list,
                        pool_entries: list, content: str,
                        admitted: list, timings: dict,
                        versions: dict) -> dict:
    from remembering.trace.canonical import (
        bundle_digest_for,
        input_digest_for,
        pool_digest_for,
    )
    from remembering.trace.lifecycle import funnel
    from remembering.trace.model import TRACE_SCHEMA_VERSION

    import hashlib as _hashlib

    query = payload.get("query", "")
    request = {
        "query": query if isinstance(query, str) else "",
        "max_chars": payload.get("max_chars", 4000),
        "max_results": payload.get("max_results", 6),
        "route_requested": payload.get("route", "auto"),
        "temporal_requested": payload.get("temporal") or {},
        "work_requested": {
            k: work_spec.get(k) for k in
            ("mode", "explicit", "prior_work_type")},
        "trust_requested": trust_spec,
        "selection_requested": selection_spec,
    }
    semantic = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "project": {
            "project_id": schema,
            "project_digest": _hashlib.sha256(
                canonical_dir.encode("utf-8")).hexdigest()[:16],
        },
        "created_at": now,
        "request": request,
        "route": route_block,
        "versions": versions,
        "stages": {
            "retrieval": retrieval_trace,
            "temporal": temporal_block,
            "frame": frame_trace,
            "trust": trust_block,
            "selection": selection_block,
            "loops": loop_block,
        },
        "candidates": lifecycles,
        "candidate_pool": {
            "entries": sorted(
                pool_entries,
                key=lambda entry: entry.get("chunk_id", "")),
            "candidate_pool_digest": pool_digest_for(sorted(
                pool_entries,
                key=lambda entry: entry.get("chunk_id", ""))),
        },
        "bundle": {
            "content": content,
            "chars": len(content),
            "render_order": [i["chunk_id"] for i in admitted],
            "bundle_digest": bundle_digest_for(content),
        },
        "funnel": funnel(lifecycles),
        "timings": timings,
        "input_digest": input_digest_for({
            "query": request["query"],
            "route": route_block.get("route"),
            "temporal": standpoint_echo,
            "work_mode": work_spec.get("mode"),
            "trust_level": trust_spec.get("level"),
            "selection_mode": selection_spec.get("mode"),
            "max_chars": request["max_chars"],
            "max_results": request["max_results"],
            "policy_versions": versions,
        }),
    }
    return semantic


def collect_versions(ret: dict, emb: dict, versions: dict,
                     frame_trace: dict, trust_block: dict,
                     selection_block: dict) -> dict:
    from remembering import ENGINE_VERSION
    from remembering.baseline.routing import ROUTING_POLICY_VERSION
    from remembering.frame.model import (
        ESTABLISHMENT_POLICY_VERSION,
        FRAME_CONTROL_POLICY_VERSION,
        FRAMING_ENGINE_VERSION,
    )
    from remembering.loops.model import (
        LOOP_CLOSURE_VERSION,
        LOOP_CONTEXT_VERSION,
        LOOP_ENGINE_VERSION,
        LOOP_EVAL_VERSION,
        LOOP_EVENT_SCHEMA,
        LOOP_REDUCER_VERSION,
    )
    from remembering.select.model import (
        BUDGET_POLICY_VERSION,
        PROVENANCE_POLICY_VERSION,
        REDUNDANCY_POLICY_VERSION,
        SELECT_ENGINE_VERSION,
        SELECTION_POLICY_VERSION,
    )
    from remembering.trace.model import (
        CANONICAL_FORMAT_VERSION,
        LIFECYCLE_SCHEMA_VERSION,
        REPLAY_PROTOCOL_VERSION,
        TRACE_ENGINE_VERSION,
        TRACE_SCHEMA_VERSION,
        TRACE_STORE_VERSION,
    )
    from remembering.trust.model import (
        INSTRUCTION_SCREEN_VERSION,
        STANDING_RESOLVER_VERSION,
        TRUST_ENGINE_VERSION,
        TRUST_POLICY_VERSION,
    )
    from remembering.write import (
        EXPLICIT_RECORD_SCHEMA,
        MEMORY_ACTION_SCHEMA,
        RELATION_VERSION,
        WRITE_ENGINE_VERSION,
        WRITE_EVAL_VERSION,
        WRITE_GATE_VERSION,
        WRITE_POLICY_SCHEMA,
        WRITE_STORE_VERSION,
    )
    return {
        "engine_version": ENGINE_VERSION,
        "retrieval": {"mode": ret.get("mode"),
                      "embedding": emb.get("model", ""),
                      "embedding_version": emb.get("version", "")},
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "temporal": {
            "store_version": versions.get("store_version"),
            "event_schema_version": versions.get(
                "event_schema_version"),
            "reducer_version": versions.get("reducer_version"),
        },
        "frame": {
            "framing_engine_version": FRAMING_ENGINE_VERSION,
            "establishment_policy": ESTABLISHMENT_POLICY_VERSION,
            "control_policy": FRAME_CONTROL_POLICY_VERSION,
            "project_frame_version": frame_trace.get(
                "project_frame_version"),
            "project_frame_digest": frame_trace.get(
                "project_frame_digest"),
        },
        "trust": {
            "trust_engine_version": TRUST_ENGINE_VERSION,
            "trust_policy_version": TRUST_POLICY_VERSION,
            "policy_version": trust_block.get("policy_version"),
            "policy_digest": trust_block.get("policy_digest"),
            "policy_source": trust_block.get("policy_source"),
            "instruction_screen_version":
                INSTRUCTION_SCREEN_VERSION,
            "standing_resolver_version": STANDING_RESOLVER_VERSION,
        },
        "selection": {
            "select_engine_version": SELECT_ENGINE_VERSION,
            "policy_version": selection_block.get("policy_version"),
            "budget_version": BUDGET_POLICY_VERSION,
            "redundancy_version": REDUNDANCY_POLICY_VERSION,
            "provenance_version": PROVENANCE_POLICY_VERSION,
        },
        "loops": {
            "loops_engine_version": LOOP_ENGINE_VERSION,
            "event_schema_version": LOOP_EVENT_SCHEMA,
            "reducer_version": LOOP_REDUCER_VERSION,
            "closure_version": LOOP_CLOSURE_VERSION,
            "context_version": LOOP_CONTEXT_VERSION,
            "eval_version": LOOP_EVAL_VERSION,
        },
        "write": {
            "write_engine_version": WRITE_ENGINE_VERSION,
            "store_version": WRITE_STORE_VERSION,
            "record_schema_version": EXPLICIT_RECORD_SCHEMA,
            "action_schema_version": MEMORY_ACTION_SCHEMA,
            "relation_version": RELATION_VERSION,
            "write_policy_schema": WRITE_POLICY_SCHEMA,
            "write_gate_version": WRITE_GATE_VERSION,
            "eval_version": WRITE_EVAL_VERSION,
        },
        "trace": {
            "trace_engine_version": TRACE_ENGINE_VERSION,
            "schema_version": TRACE_SCHEMA_VERSION,
            "store_version": TRACE_STORE_VERSION,
            "canonical_format": CANONICAL_FORMAT_VERSION,
            "lifecycle_version": LIFECYCLE_SCHEMA_VERSION,
            "replay_protocol": REPLAY_PROTOCOL_VERSION,
        },
    }


def persist_durable_trace(connection, schema: str, semantic: dict,
                          bundle_content: str,
                          pool_entries: list) -> dict:
    """Persist one trace atomically. Returns ids + persisted flag."""
    import datetime

    from remembering.trace import postgres as trace_pg

    trace_pg.initialise(connection, schema)
    created_at = semantic.get("created_at") or datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    lifecycles = semantic.get("candidates", [])
    policies = _policy_rows(semantic)
    return trace_pg.insert_trace(
        connection, schema, semantic, bundle_content, pool_entries,
        created_at, lifecycles, policies)


def _policy_rows(semantic: dict) -> list[dict]:
    versions = semantic.get("versions", {})
    rows: list[dict] = []

    def add(stage: str, name: str, version, digest) -> None:
        rows.append({"stage": stage, "policy_name": name,
                     "version": str(version or "unknown"),
                     "digest": str(digest or version or "unknown")})

    retrieval = versions.get("retrieval", {})
    add("retrieval", "retrieval",
        retrieval.get("embedding_version"), retrieval.get("mode"))
    add("routing", "routing",
        versions.get("routing_policy_version"), None)
    temporal = versions.get("temporal", {})
    add("temporal", "reducer", temporal.get("reducer_version"), None)
    frame = versions.get("frame", {})
    add("frame", "project_frame", frame.get("project_frame_version"),
        frame.get("project_frame_digest"))
    add("frame", "establishment", frame.get("establishment_policy"), None)
    add("frame", "control", frame.get("control_policy"), None)
    trust = versions.get("trust", {})
    add("trust", "trust_policy", trust.get("policy_version"),
        trust.get("policy_digest"))
    add("trust", "instruction_screen",
        trust.get("instruction_screen_version"), None)
    selection = versions.get("selection", {})
    add("selection", "selection", selection.get("policy_version"), None)
    add("selection", "budget", selection.get("budget_version"), None)
    return rows


def do_context(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.baseline.routing import guidance_for, route_for_request

    pipeline_started = time.perf_counter()
    schema = schema_from(payload)
    max_chars = payload.get("max_chars", 4000)
    max_results = payload.get("max_results", 6)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) \
            or max_chars < 1:
        raise BridgeError("CONFIG_INVALID",
                          "max_chars must be a positive integer")
    if not isinstance(max_results, int) or isinstance(max_results, bool) \
            or max_results < 1:
        raise BridgeError("CONFIG_INVALID",
                          "max_results must be a positive integer")
    requested = route_requested(payload)
    query = payload.get("query", "")
    route_started = time.perf_counter()
    try:
        decision = route_for_request(
            query if isinstance(query, str) else "", requested)
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
    route_ms = round((time.perf_counter() - route_started) * 1000, 2)
    route_block = decision.to_dict()
    guidance = guidance_for(decision.route)

    search_payload = dict(payload)
    search_payload["limit"] = min(max_results, 20)
    result = do_search(search_payload)
    trace = result.get("trace")
    if isinstance(trace, dict):
        trace = {**trace, "route": route_block}

    requested_temporal = payload.get("temporal") or {}
    if not isinstance(requested_temporal, dict):
        raise BridgeError("CONFIG_INVALID", "temporal must be an object")
    try:
        from remembering.baseline.temporal import (
            standpoint_for_route as _sfr,
            utcnow_iso as _now,
        )

        now = _now()
        standpoint = _sfr(decision.route, requested_temporal, now)
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
    standpoint_echo = {
        "mode": standpoint.mode,
        "valid_at": standpoint.valid_at,
        "known_at": standpoint.known_at,
    }

    if not result["indexed"]:
        return {**result, "trace": trace,
                "trace_id": "hybrid:unindexed", "content": "",
                "chars": 0, "route": route_block,
                "temporal": standpoint_echo,
                "frame": {"applied": False,
                          "reason": "frame.store_unindexed"},
                "trust": {"mode": "none", "admitted": 0, "denied": 0,
                          "quarantined": 0},
                "admission_note": admission_note_for(decision.route)}
    if not result["trace"]:
        return {**result, "trace": trace,
                "trace_id": "hybrid:empty-query", "content": "",
                "chars": 0, "route": route_block,
                "temporal": standpoint_echo,
                "frame": {"applied": False,
                          "reason": "frame.empty_query"},
                "trust": {"mode": "none", "admitted": 0, "denied": 0,
                          "quarantined": 0},
                "admission_note": admission_note_for(decision.route)}

    from remembering.baseline.storage import ScoredChunk
    from remembering.baseline.temporal import (
        admit_for_route,
        annotate_candidates,
        resolve_subject,
    )
    from remembering.temporal import postgres as temporal_pg

    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)

    # Framing applies to INFLUENCE only, after temporal resolution.
    # Recall bypasses frame control (isolation aside): current work
    # must not erase history.
    frame_block: dict[str, Any] = {"applied": False,
                                   "reason": "frame.recall_bypass"}
    work_spec = parse_work_spec(payload)
    if decision.route.value == "influence" and work_spec["mode"] != "none":
        frame_block = frame_context_for(schema, project_dir,
                                        canonical_dir, work_spec, now)
    frame_applied = frame_block.get("applied") is True
    frame_control = frame_block.get("control", "query_only")

    # Frame-conditioned retrieval expansion: the objective may surface
    # evidence the literal query missed. The query path always stays
    # represented; both paths are traced separately.
    retrieval_paths: dict[str, Any] = {
        "query": {"ids": [c["chunk_id"] for c in result["items"]],
                  "latencies_ms": result["trace"]["latencies_ms"]},
        "objective": None,
        "fused_pool_ids": [c["chunk_id"] for c in result["items"]],
    }
    pool_items: list[dict[str, Any]] = list(result["items"])
    objective = (frame_block.get("establishment", {}) or {}).get(
        "objective", "") if frame_applied else ""
    if frame_applied and frame_control in ("hard", "soft") and objective:
        from remembering.baseline.retrieval import (
            reciprocal_rank_fusion as _rrf,
        )

        expansion_started = time.perf_counter()
        objective_payload = dict(search_payload)
        objective_payload["query"] = objective
        objective_result = do_search(objective_payload)
        expansion_ms = round(
            (time.perf_counter() - expansion_started) * 1000, 2)
        if objective_result.get("trace"):
            retrieval_paths["objective"] = {
                "query": objective,
                "ids": objective_result["trace"]["reranked_ids"],
                "latencies_ms": objective_result["trace"]["latencies_ms"],
            }
            by_id = {c["chunk_id"]: c for c in pool_items}
            for item in objective_result["items"]:
                by_id.setdefault(item["chunk_id"], item)
            fused = _rrf([
                [ScoredChunk(c["chunk_id"], c["source_id"], c["text"],
                             c["section"], c["score"], c["rank"])
                 for c in pool_items],
                [ScoredChunk(c["chunk_id"], c["source_id"], c["text"],
                             c["section"], c["score"], c["rank"])
                 for c in objective_result["items"]],
            ], k=ret_fusion_k(search_payload))
            pool_items = [by_id[c.chunk_id] for c in fused
                          if c.chunk_id in by_id]
            retrieval_paths["fused_pool_ids"] = [c["chunk_id"]
                                                 for c in pool_items]
            trace["latencies_ms"] = {
                **trace["latencies_ms"],
                "objective_expansion": expansion_ms,
            }

    # Relevant open-loop pull (influence only): durable unfinished
    # work relevant to the current objective joins the pool as derived
    # candidates with provenance. They flow through temporal, trust,
    # and selection like everything else — no invisible channel.
    # Recall never pulls loops; history questions use memory_open_loops.
    loop_block: dict[str, Any] = {"considered": [], "states": {},
                                  "derived_ids": [],
                                  "search_complete": True,
                                  "reason": "loops.recall_bypass"}
    if decision.route.value == "influence":
        from remembering.loops.context import (
            loop_candidate,
            relevant_loops,
        )
        from remembering.loops.model import LOOP_CONTEXT_VERSION

        loop_started = time.perf_counter()
        loop_connection = raw_connect()
        try:
            loop_loaded = load_loop_views(loop_connection, schema,
                                          project_dir)
        finally:
            loop_connection.close()
        loop_views = loop_loaded["views"]
        objective_text = objective or (
            query if isinstance(query, str) else "")
        pool_sources = {c["source_id"] for c in pool_items}
        relevant = relevant_loops(
            loop_views, objective=objective_text,
            evidence_sources=pool_sources, limit=3)
        derived_ids: list[str] = []
        for loop_id in relevant:
            view = loop_views.get(loop_id)
            if view is None:
                continue
            rendered = loop_candidate(view, pool_sources)
            if any(c["chunk_id"] == rendered["chunk_id"]
                   for c in pool_items):
                continue
            pool_items.append({
                "chunk_id": rendered["chunk_id"],
                "source_id": rendered["source_id"],
                "section": None,
                "rank": len(pool_items) + 1,
                "score": 0.0,
                "text": rendered["text"],
                "lexical_rank": None,
                "dense_rank": None,
                "stage": "loop-pull",
                "loop_claim_key": rendered["claim_key"],
                "loop_derived_from": rendered["derived_from"],
                "loop_role": rendered["role"],
            })
            derived_ids.append(rendered["chunk_id"])
        loop_block = {
            "considered": sorted(loop_views.keys()),
            "states": {lid: view.state.value
                       for lid, view in loop_views.items()},
            "derived_ids": derived_ids,
            "search_complete": True,
            "evidence_boundary": "loop events + temporal log + pool "
                                 "sources",
            "reason": "loops.pulled" if derived_ids else "loops.no_match",
            "projection_version": loop_loaded["versions"].get(
                "projection_version"),
            "context_version": LOOP_CONTEXT_VERSION,
            "latency_ms": round(
                (time.perf_counter() - loop_started) * 1000, 2),
        }
    retrieval_paths["loop_derived_ids"] = loop_block["derived_ids"]

    loop_claims: dict[str, dict] = {}
    loop_meta: dict[str, dict] = {}
    for item in pool_items:
        if item.get("stage") != "loop-pull":
            continue
        loop_claims[item["source_id"]] = {
            "claim_key": item.get("loop_claim_key", ""),
            "derived_from": item.get("loop_derived_from", []),
            "refuted_by": [],
            "role": item.get("loop_role", "ordinary"),
        }
        loop_meta[item["chunk_id"]] = loop_claims[item["source_id"]]

    # Temporal interpretation runs after retrieval and before
    # admission: retrieval proposes, temporal admission decides
    # current standing. Rank, mtime, and arrival order confer nothing.
    temporal_started = time.perf_counter()
    connection = raw_connect()
    try:
        log = load_temporal_log(connection, schema)
        versions = temporal_pg.versions(connection, schema)
    finally:
        connection.close()

    ranked = [ScoredChunk(c["chunk_id"], c["source_id"], c["text"],
                          c["section"], c["score"], c["rank"])
              for c in pool_items]
    annotated = annotate_candidates(
        ranked, log, now=standpoint.known_at or now)
    by_annotation = {a.chunk_id: a for a in annotated}
    # Stage 9 relationship overlay: explicit-memory lifecycle
    # relations adjust annotation under the same standpoint before
    # the existing admission runs. Recall still preserves all.
    from remembering.write.overlay import (
        apply_relationship_overlay as _apply_overlay,
    )

    _overlay_data = load_write_overlay(schema)
    write_overlay = _apply_overlay(
        annotated, _overlay_data["relations"],
        _overlay_data["records"], standpoint, now)
    subjects = sorted({a.subject for a in annotated if a.subject})
    resolved = {s: resolve_subject(log, s, standpoint) for s in subjects}
    selected, suppressed = admit_for_route(annotated, decision.route)
    temporal_ms = round((time.perf_counter() - temporal_started) * 1000, 2)

    # Frame-conditioned candidate handling (influence only, frame
    # applied). Stages stay separate: temporal eligibility first,
    # then frame eligibility/ordering, then budget.
    frame_selected_ids: set[str] | None = None
    frame_excluded: list[dict[str, Any]] = []
    frame_latency_ms = 0.0
    if frame_applied and decision.route.value == "influence":
        from remembering.baseline.storage import Store
        from remembering.frame.model import FrameControl
        from remembering.frame.policy import (
            FramedCandidate,
            apply_scope,
            order_and_select,
        )
        from remembering.frame.signals import evidence_class

        frame_started = time.perf_counter()
        store = Store(dsn(), schema)
        try:
            artifact_map = store.artifact_types(
                list({c["source_id"] for c in pool_items}))
        finally:
            store.close()
        framed = [FramedCandidate(
            chunk_id=c["chunk_id"], source_id=c["source_id"],
            evidence_class=evidence_class(
                c["source_id"], artifact_map.get(c["source_id"])))
            for c in pool_items
            if c["chunk_id"] in {s.chunk_id for s in selected}]
        if frame_block.get("include_projects"):
            apply_scope(framed, tuple(frame_block["include_projects"]))
        kept, dropped = order_and_select(
            framed,
            tuple(frame_block.get("preferred_classes", ())),
            FrameControl(frame_control),
            bool(frame_block.get("hard_exclude", False)))
        frame_selected_ids = {c.chunk_id for c in kept}
        frame_excluded = [c.to_dict() for c in dropped]
        frame_latency_ms = round(
            (time.perf_counter() - frame_started) * 1000, 2)
    else:
        kept = []

    selected_ids = {s.chunk_id for s in selected}
    if frame_selected_ids is not None:
        selected_ids &= frame_selected_ids
    if frame_applied and decision.route.value == "influence":
        framed_order = [c.chunk_id for c in kept]
        order_index = {cid: i for i, cid in enumerate(framed_order)}
        ranked_selected = sorted(
            [c for c in ranked if c.chunk_id in selected_ids],
            key=lambda c: order_index.get(c.chunk_id, len(order_index)))
    else:
        ranked_selected = [c for c in ranked if c.chunk_id in selected_ids]

    # Trust/standing gate: the last influence control before budget.
    # Relevant + current + framed still needs permission to steer
    # behaviour. Recall annotates verdicts without excluding.
    trust_spec = parse_trust_spec(payload)
    enforce_trust = decision.route.value == "influence"
    trust_out = apply_trust_gate(
        schema, project_dir, ranked_selected, by_annotation,
        frame_control=(frame_block.get("control", "query_only")
                       if frame_applied else "query_only"),
        route_value=decision.route.value, trust_spec=trust_spec,
        enforce=enforce_trust, loop_meta=loop_meta)
    trust_block = trust_out["block"]
    trust_records = trust_out["records_by_id"]
    trust_classes = trust_out["classes_by_id"]
    if enforce_trust:
        admitted_trust = trust_out["admitted_ids"]
        ranked_selected = [c for c in ranked_selected
                           if c.chunk_id in admitted_trust]
    trace_trust_latency = trust_block["latency_ms"]

    # Decisive selection: admission is permission, selection is
    # necessity. The render loop below enforces the byte budget over
    # the selected order; truncation is flagged, never silent.
    selection_spec = parse_selection_spec(payload)
    framed_by_id = {c.chunk_id: c for c in kept}
    selection_out = apply_selection(
        schema, project_dir, ranked_selected, pool_items, by_annotation,
        trust_records, trust_classes, trust_out["roles_by_id"],
        framed_by_id,
        frame_control=(frame_block.get("control", "query_only")
                       if frame_applied else "query_only"),
        route_value=decision.route.value, selection_spec=selection_spec,
        enforce_trust=enforce_trust,
        constraints=(frame_block.get("project_constraints", [])
                     if frame_applied else []),
        claims_map={**trust_out["claims_map"], **loop_claims},
        max_chars=max_chars, max_results=max_results,
        retrieval_paths=retrieval_paths)
    selection_block = selection_out["block"]
    selection_records = selection_out["records_by_id"]
    ordered_chunks = selection_out["ordered"]
    ordered_ids = [c.chunk_id for c in ordered_chunks]
    bundle_dropped_duplicates = selection_block["dropped_redundant"]
    bundle_dropped_over_budget = selection_block["dropped_budget"]
    # Route guidance is bundle metadata, but it still counts toward the
    # byte budget: the bundle as a whole stays bounded.
    guidance_prefix = guidance + "\n\n---\n\n"
    evidence_budget = max_chars - len(guidance_prefix)
    parts: list[str] = []
    admitted: list[dict[str, Any]] = []
    by_id = {c["chunk_id"]: c for c in pool_items}
    chars = 0
    if evidence_budget > 0:
        for candidate in ordered_chunks:
            chunk = next(c for c in ranked_selected
                         if c.chunk_id == candidate.chunk_id)
            item = by_id.get(chunk.chunk_id, {
                "chunk_id": chunk.chunk_id, "source_id": chunk.source_id,
                "section": chunk.section, "rank": chunk.rank,
                "score": chunk.score, "text": chunk.text,
                "lexical_rank": None, "dense_rank": None,
                "stage": "admitted"})
            annotation = by_annotation.get(chunk.chunk_id)
            temporal_status = (annotation.status if annotation
                               else "not_modelled")
            frame_tag = ""
            if frame_applied and decision.route.value == "influence":
                framed_item = framed_by_id.get(chunk.chunk_id)
                if framed_item is not None:
                    frame_tag = (f" | frame: "
                                 f"{framed_item.evidence_class}")
            trust_record = trust_records.get(chunk.chunk_id)
            standing_tag = ""
            if trust_record is not None and enforce_trust:
                standing_tag = (
                    f" | standing: "
                    f"{trust_classes.get(chunk.chunk_id, 'unknown')}")
            explicit_tag = ""
            if item["source_id"].startswith("memory://explicit/"):
                explicit_tag = (
                    f" | role: "
                    f"{trust_out['roles_by_id'].get(chunk.chunk_id, 'ordinary')}")
            section = f" | {item['section']}" if item["section"] else ""
            header = (f"[source: {item['source_id']}{section} | "
                      f"chunk: {item['chunk_id']} | rank: {item['rank']} | "
                      f"score: {item['score']:.4f} | "
                      f"temporal: {temporal_status}{frame_tag}"
                      f"{standing_tag}{explicit_tag}]\n")
            separator = "\n\n---\n\n" if parts else ""
            text = item["text"]
            # assemble() always admits the top-ranked chunk even when it
            # alone exceeds the budget (code files chunk large under the
            # sentence policy). Enforce the evidence budget here instead
            # of emitting an unbounded bundle: truncate with an explicit
            # marker, never silently. Provenance headers are never
            # truncated.
            room = evidence_budget - chars - len(separator) - len(header)
            if room < 0:
                break
            truncated = False
            original_chars = len(text)
            if len(text) > room:
                marker = "\n[…truncated to fit the context budget…]"
                text = text[:max(0, room - len(marker))] + marker
                truncated = True
            piece = separator + header + text
            parts.append(piece)
            chars += len(piece)
            admitted_item = {**item, "text": text}
            if annotation is not None:
                admitted_item["temporal"] = annotation.to_dict()
            if trust_record is not None:
                admitted_item["trust"] = trust_record.to_dict()
            selection_record = selection_records.get(chunk.chunk_id)
            if selection_record is not None:
                selection_record.truncated = truncated
                selection_record.original_chars = original_chars
                selection_record.chars = len(text)
                admitted_item["selection"] = selection_record.to_dict()
            admitted.append(admitted_item)
            if chars >= evidence_budget:
                break

    temporal_block = {
        "mode": standpoint.mode,
        "valid_at": standpoint.valid_at,
        "known_at": standpoint.known_at,
        "subjects": resolved,
        "suppressed": [s.to_dict() for s in suppressed],
        "annotations": [a.to_dict() for a in annotated],
        "write_overlay": write_overlay,
        "selected_ids": [i["chunk_id"] for i in admitted],
        "dropped_duplicates": bundle_dropped_duplicates,
        "dropped_over_budget": bundle_dropped_over_budget,
        "incomplete_history": any(a.incomplete_history
                                  for a in annotated),
        "store_version": versions.get("store_version"),
        "event_schema_version": versions.get("event_schema_version"),
        "reducer_version": versions.get("reducer_version"),
        "latency_ms": temporal_ms,
    }
    frame_trace = dict(frame_block)
    frame_trace["excluded"] = frame_excluded
    frame_trace["latency_ms"] = frame_latency_ms
    frame_trace["retrieval_paths"] = retrieval_paths
    trace = {**trace, "temporal": temporal_block, "frame": frame_trace,
             "trust": trust_block, "selection": selection_block,
             "loops": loop_block}

    # Durable ContextTrace: build the immutable record, persist it,
    # then hand the caller its content-addressed ID. Influence
    # requires persistence; recall degrades to an unpersisted trace.
    from remembering.trace.canonical import (
        bundle_digest_for,
        content_hash,
    )
    from remembering.trace.lifecycle import build_lifecycles, funnel

    trace_started = time.perf_counter()
    retrieval_index: dict[str, dict] = {}
    lexical_ids = set(result["trace"].get("lexical_ids", []))
    dense_ids = set(result["trace"].get("dense_ids", []))
    fused_ids = result["trace"].get("fused_ids", [])
    fused_rank = {cid: i + 1 for i, cid in enumerate(fused_ids)}
    objective_ids = set(
        ((retrieval_paths.get("objective") or {}) or {}).get("ids", []))
    for item in pool_items:
        paths = []
        if item["chunk_id"] in lexical_ids:
            paths.append("lexical")
        if item["chunk_id"] in dense_ids:
            paths.append("dense")
        if item["chunk_id"] in objective_ids:
            paths.append("objective")
        if item.get("stage") == "loop-pull":
            paths.append("loop")
        retrieval_index[item["chunk_id"]] = {
            "lexical_rank": item.get("lexical_rank"),
            "dense_rank": item.get("dense_rank"),
            "fused_rank": fused_rank.get(item["chunk_id"]),
            "paths": paths,
            "score": item.get("score"),
        }
    temporal_selected_ids = {s.chunk_id for s in selected}
    frame_kept_ids = {c.chunk_id for c in kept} if frame_applied else None
    rendered_ids = [i["chunk_id"] for i in admitted]
    trust_inputs: dict[str, dict] = {}
    ceiling_map = trust_out.get("ceiling_by_id", {})
    for item in pool_items:
        claim = (trust_out["claims_map"] or {}).get(item["source_id"], {})
        ceiling_info = ceiling_map.get(item["chunk_id"], {})
        trust_inputs[item["chunk_id"]] = {
            "source_class": trust_classes.get(item["chunk_id"],
                                              "informational"),
            "role": trust_out["roles_by_id"].get(item["chunk_id"],
                                                 "ordinary"),
            "claim_key": claim.get("claim_key", ""),
            "derived_from": claim.get("derived_from", []),
            "refuted_by": claim.get("refuted_by", []),
            "restricted_to": trust_out.get("restricted_by_id", {}).get(
                item["chunk_id"], []),
            "write_ceiling": ceiling_info.get("write_ceiling"),
            "trust_policy_class": ceiling_info.get(
                "trust_policy_class"),
        }
    lifecycles = build_lifecycles(
        pool_items, temporal_selected_ids, by_annotation, frame_kept_ids,
        frame_applied, trust_records, enforce_trust, selection_records,
        rendered_ids, retrieval_index, trust_inputs)
    pool_entries = [
        {"chunk_id": c["chunk_id"],
         "content_hash": content_hash(c.get("text", ""))}
        for c in pool_items]
    emb_spec = embedding_spec_from(payload)
    ret_spec = retrieval_spec_from(payload)
    version_block = collect_versions(
        ret_spec,
        {"model": emb_spec.get("model", ""),
         "version": result["trace"]["embedding"]["version"]},
        versions, frame_trace, trust_block, selection_block)
    timings = {
        "route_ms": route_ms,
        "retrieval_ms": result["trace"]["latencies_ms"],
        "temporal_ms": temporal_ms,
        "frame_ms": frame_latency_ms,
        "trust_ms": trust_block.get("latency_ms"),
        "selection_ms": selection_block.get("latency_ms"),
    }
    semantic = build_durable_trace(
        schema, canonical_dir, now, payload, route_block, standpoint_echo,
        work_spec, trust_spec, selection_spec, result["trace"],
        temporal_block, frame_trace, trust_block, selection_block,
        loop_block, lifecycles, pool_entries,
        "", admitted, timings, version_block)
    evidence = "\n\n---\n\n".join(parts)
    content = guidance + "\n\n---\n\n" + evidence if evidence else guidance
    # Bundle content is fixed before persistence so the stored digest
    # covers exactly what the caller receives.
    semantic["bundle"] = {
        "content": content,
        "chars": len(content),
        "render_order": [i["chunk_id"] for i in admitted],
        "bundle_digest": bundle_digest_for(content),
    }
    semantic["funnel"] = funnel(lifecycles)
    from remembering.trace.postgres import compute_ids as _compute_ids

    pre_ids = _compute_ids(semantic, content, pool_entries)
    persist_started = time.perf_counter()
    connection = raw_connect()
    try:
        persist_out = persist_durable_trace(
            connection, schema, semantic, content, pool_entries)
    except ValueError as exc:
        connection.close()
        raise BridgeError("TRACE_STORE_ERROR", str(exc))
    except Exception as exc:
        connection.close()
        if enforce_trust:
            raise BridgeError(
                "TRACE_PERSIST_FAILED",
                f"durable trace persistence failed: "
                f"{type(exc).__name__}: {str(exc)[:160]}; refusing to "
                "inject unaudited influential memory")
        trace_persisted = False
        persist_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        persist_out = {"trace_id": pre_ids["trace_id"], **pre_ids}
    else:
        connection.close()
        trace_persisted = True
        persist_error = None
    timings["trace_construction_ms"] = round(
        (time.perf_counter() - trace_started) * 1000, 2)
    timings["trace_persistence_ms"] = round(
        (time.perf_counter() - persist_started) * 1000, 2)
    timings["total_context_ms"] = round(
        (time.perf_counter() - pipeline_started) * 1000, 2)
    semantic["timings"] = timings
    trace_id = persist_out["trace_id"]
    trace = {**trace, "durable_trace_id": trace_id,
             "trace_persisted": trace_persisted,
             "timings": timings,
             "candidates": lifecycles,
             "funnel": semantic["funnel"],
             "bundle": semantic["bundle"],
             "bundle_digest": persist_out["bundle_digest"],
             "candidate_pool_digest": persist_out[
                 "candidate_pool_digest"],
             "input_digest": persist_out["input_digest"]}
    if persist_error is not None:
        trace["persist_error"] = persist_error
    denied_n = len(trust_block.get("denied_ids", []))
    quarantined_n = len(trust_block.get("quarantined_ids", []))
    trust_note = ""
    if enforce_trust and (denied_n or quarantined_n):
        trust_note = (f" Trust excluded {denied_n} denied and "
                      f"{quarantined_n} quarantined candidate(s); "
                      "see trace.trust.records.")
    return {
        "ok": True, "indexed": True, "schema": result["schema"],
        "items": admitted, "trace": trace,
        "trace_id": trace_id, "content": content,
        "chars": len(content), "route": route_block,
        "trace_persisted": trace_persisted,
        "temporal": {
            "mode": standpoint.mode,
            "valid_at": standpoint.valid_at,
            "known_at": standpoint.known_at,
        },
        "frame": {k: frame_trace.get(k) for k in (
            "applied", "reason", "control", "control_reason",
            "establishment", "project_frame_version",
            "project_frame_digest", "excluded")},
        "trust": {
            "mode": trust_block.get("mode"),
            "level": trust_block.get("level"),
            "policy_version": trust_block.get("policy_version"),
            "policy_source": trust_block.get("policy_source"),
            "admitted": len(trust_block.get("admitted_ids", [])),
            "denied": denied_n,
            "quarantined": quarantined_n,
        },
        "selection": {
            "policy_version": selection_block.get("policy_version"),
            "input_count": selection_block.get("input_count"),
            "selected_count": selection_block.get("selected_count"),
            "dropped_redundant": selection_block.get("dropped_redundant"),
            "dropped_low_value": selection_block.get(
                "dropped_low_value", 0),
            "dropped_budget": selection_block.get("dropped_budget"),
            "chars_before": selection_block.get("chars_before"),
            "chars_after": selection_block.get("chars_after"),
            "compression_ratio": selection_block.get("compression_ratio"),
            "budget_insufficient": selection_block.get(
                "budget_insufficient"),
        },
        "admission_note": admission_note_for(
            decision.route, suppressed=len(suppressed)) + trust_note,
    }


def route_requested(payload: dict[str, Any]) -> str:
    """Explicit route request: auto (default), recall, or influence.

    Anything else fails closed; historical-ish strings are never
    silently reinterpreted."""
    value = payload.get("route", "auto")
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise BridgeError("CONFIG_INVALID", "route must be a string")
    normalized = value.strip().lower()
    if normalized in ("auto", "recall", "influence"):
        return normalized
    raise BridgeError(
        "CONFIG_INVALID",
        f"unknown route {value!r}: expected 'auto', 'recall' or "
        "'influence'.",
    )


def admission_note_for(route, suppressed: int = 0) -> str:
    from remembering.baseline.routing import MemoryRoute

    base = (
        "Retrieval evidence only. No frame or trust admission has been "
        "applied at this stage; temporal validity does not imply "
        "trustworthiness. Superseded history may still be valid for "
        "historical recall."
    )
    if route is MemoryRoute.RECALL:
        return ("Recall bundle: historical reconstruction; temporal "
                "relations interpreted, nothing suppressed for being "
                f"non-current. {base}")
    extra = (f" {suppressed} retrieved candidate(s) suppressed as "
             "superseded-as-current." if suppressed else
             " No retrieved candidate met the bar for temporal suppression.")
    return ("Influence bundle: resolved toward applicable current state. "
            f"{base}{extra}")


def do_temporal_import(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.temporal_events",))
            if cur.fetchone()[0] is None:
                return {"ok": False, "schema": schema,
                        "message": ("temporal storage is not initialised; "
                                    "run memory_setup or memory_refresh."),
                        "imported": 0, "duplicates": 0, "failed": [],
                        "events": 0, "subjects": []}
            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}, "
                    f"not {canonical_dir!r}. Refusing to touch another "
                    "project's temporal history.")
        report = import_temporal_events(connection, schema, project_dir)
    finally:
        connection.close()
    ok = not report["failed"]
    return {"ok": ok, "schema": schema,
            "message": ("imported "
                        f"{report['imported']} event(s), "
                        f"{report['duplicates']} duplicate(s)"
                        + ("" if ok else
                           f"; failures: {report['failed']}")),
            **report}


def do_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Inspect resolved temporal state for one subject."""
    require_engine()
    from remembering.baseline.temporal import (
        resolve_subject,
        standpoint_for_route,
        utcnow_iso,
    )
    from remembering.baseline.routing import route_for_request

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    subject = payload.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise BridgeError("CONFIG_INVALID",
                          "subject is required and must not be empty")
    subject = subject.strip()
    requested = route_requested(payload)
    query = payload.get("query", "")
    try:
        decision = route_for_request(
            query if isinstance(query, str) else "", requested)
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
    requested_temporal = payload.get("temporal") or {}
    if not isinstance(requested_temporal, dict):
        raise BridgeError("CONFIG_INVALID", "temporal must be an object")
    now = utcnow_iso()
    try:
        standpoint = standpoint_for_route(decision.route,
                                          requested_temporal, now)
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))

    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        log = load_temporal_log(connection, schema)
        from remembering.temporal import postgres as temporal_pg

        versions = temporal_pg.versions(connection, schema)
    finally:
        connection.close()
    resolved = resolve_subject(log, subject, standpoint)
    resolved["route"] = decision.to_dict()
    resolved["store_version"] = versions.get("store_version")
    resolved["event_schema_version"] = versions.get("event_schema_version")
    resolved["reducer_version"] = versions.get("reducer_version")
    return {"ok": True, "schema": schema, **resolved}


def do_temporal_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 3 temporal contract (no services)."""
    _ = payload
    require_engine()
    from remembering.baseline.temporal import evaluate_temporal

    report = evaluate_temporal()
    return {"ok": bool(report["passed"]), **report}


def do_trust_import(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.standing_events",))
            if cur.fetchone()[0] is None:
                return {"ok": False, "schema": schema,
                        "message": ("standing storage is not initialised; "
                                    "run memory_setup or memory_refresh."),
                        "imported": 0, "duplicates": 0, "failed": [],
                        "events": 0}
            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        report = import_standing_events(connection, schema, project_dir)
    finally:
        connection.close()
    ok = not report["failed"]
    return {"ok": ok, "schema": schema,
            "message": (f"imported {report['imported']} standing "
                        f"event(s), {report['duplicates']} duplicate(s)"
                        + ("" if ok else
                           f"; failures: {report['failed']}")),
            **report}


def do_frame_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 4 frame contract (no services)."""
    _ = payload
    require_engine()
    from remembering.frame.evaluation import evaluate_frame

    report = evaluate_frame()
    return {"ok": bool(report["passed"]), **report}


def do_trust_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 5 trust contract + ladder (no services)."""
    _ = payload
    require_engine()
    from remembering.trust.evaluation import (
        evaluate_ladder,
        evaluate_trust,
    )

    contract = evaluate_trust()
    ladder = evaluate_ladder()
    return {"ok": bool(contract["passed"]), "contract": contract,
            "ladder": ladder}


def do_loop_import(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.open_loop_events",))
            if cur.fetchone()[0] is None:
                return {"ok": False, "schema": schema,
                        "message": ("loop storage is not initialised; "
                                    "run memory_setup or memory_refresh."),
                        "imported": 0, "duplicates": 0, "failed": [],
                        "events": 0, "loops": []}
            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        report = import_loop_events(connection, schema, project_dir)
    finally:
        connection.close()
    ok = not report["failed"]
    return {"ok": ok, "schema": schema,
            "message": (f"imported {report['imported']} loop "
                        f"event(s), {report['duplicates']} duplicate(s)"
                        + ("" if ok else
                           f"; failures: {report['failed']}")),
            **report}


def do_selection_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 6 selection contract (no services)."""
    _ = payload
    require_engine()
    from remembering.select.evaluation import evaluate_selection

    report = evaluate_selection()
    return {"ok": bool(report["passed"]), **report}


def do_loops(payload: dict[str, Any]) -> dict[str, Any]:
    """memory_open_loops: list | get | history over durable loop state."""
    require_engine()
    from remembering.loops.model import LOOP_ENGINE_VERSION
    from remembering.loops.query import (
        current_loops,
        explain_loop,
        filter_loops,
    )

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    action = payload.get("action", "list")
    if not isinstance(action, str) or action.strip().lower() not in (
            "list", "get", "history"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown loops action {payload.get('action')!r}: expected "
            "'list', 'get' or 'history'.")
    action = action.strip().lower()
    limit = payload.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise BridgeError("CONFIG_INVALID", "limit must be an integer")
    limit = max(1, min(limit, 100))

    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.open_loop_events",))
            if cur.fetchone()[0] is None:
                return {"ok": True, "schema": schema, "action": action,
                        "loops": [], "loop": None,
                        "message": "loop storage is not initialised; "
                                   "run memory_setup."}
            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        valid_at = payload.get("valid_at")
        known_at = payload.get("known_at")
        for label, value in (("valid_at", valid_at),
                             ("known_at", known_at)):
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise BridgeError(
                        "CONFIG_INVALID",
                        f"{label} must be a non-empty string")
                from remembering.temporal.ordering import (
                    parse_ts as _parse_ts,
                )

                if _parse_ts(value.strip()) is None:
                    raise BridgeError(
                        "CONFIG_INVALID",
                        f"TEMPORAL_BAD_TIMESTAMP:{label}={value!r}")
        loaded = load_loop_views(connection, schema, project_dir,
                                 valid_at=valid_at, known_at=known_at)
    finally:
        connection.close()
    views = loaded["views"]
    events = loaded["events"]
    versions = loaded["versions"]

    if action == "list":
        state = payload.get("state")
        if state is not None and not isinstance(state, str):
            raise BridgeError("CONFIG_INVALID",
                              "state filter must be a string")
        include_uncertain = True
        if state is not None and state.strip().lower() in (
                "open", "uncertain", "completed", "cancelled",
                "superseded"):
            matched = filter_loops(
                views, state=state.strip().lower(), limit=limit)
        else:
            if state is not None:
                raise BridgeError(
                    "CONFIG_INVALID",
                    f"unknown loop state {state!r}.")
            subject = payload.get("subject")
            kind = payload.get("transition_kind")
            matched = filter_loops(
                views,
                subject=subject if isinstance(subject, str) else None,
                transition_kind=kind if isinstance(kind, str) else None,
                limit=limit)
            if state is None:
                matched = [v for v in matched
                           if v.state.value in ("open", "uncertain")]
        return {"ok": True, "schema": schema, "action": "list",
                "loops": [v.to_dict() for v in matched],
                "versions": versions,
                "engine_version": LOOP_ENGINE_VERSION}
    loop_id = payload.get("loop_id")
    if not isinstance(loop_id, str) or not loop_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          f"action {action!r} requires loop_id")
    loop_id = loop_id.strip()
    view = views.get(loop_id)
    if view is None:
        return {"ok": False, "schema": schema, "action": action,
                "message": "LOOP_NOT_FOUND", "loop": None}
    return {"ok": True, "schema": schema, "action": action,
            "loop": explain_loop(view, events),
            "versions": versions,
            "engine_version": LOOP_ENGINE_VERSION}


def do_loop_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 8 loop contract (no services)."""
    _ = payload
    require_engine()
    from remembering.loops.evaluation import evaluate_loops

    report = evaluate_loops()
    return {"ok": bool(report["passed"]), **report}


def do_loop_rebuild(payload: dict[str, Any]) -> dict[str, Any]:
    """Delete and rebuild the derived loop projection from events."""
    require_engine()
    from remembering.loops import postgres as loops_pg
    from remembering.loops.evidence import make_resolver
    from remembering.loops.reducer import project
    from remembering.temporal import postgres as temporal_pg
    from remembering.temporal.query import TemporalEngine

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        before = load_loop_views(connection, schema, project_dir)
        before_states = {lid: view.state.value
                         for lid, view in before["views"].items()}
        events = before["events"]
        from psycopg import sql as _sql

        with connection.cursor() as cur:
            cur.execute(_sql.SQL("DELETE FROM {}.open_loops").format(
                _sql.Identifier(schema)))
        temporal_log = temporal_pg.load_log(connection, schema)
        engine = TemporalEngine(temporal_log)
        with connection.cursor() as cur:
            cur.execute(_sql.SQL("SELECT source_id FROM {}.sources").format(
                _sql.Identifier(schema)))
            known = {row[0] for row in cur.fetchall()}
        resolve = make_resolver(temporal=engine, known_sources=known,
                                loop_events=events)
        rebuilt = project(events, resolve)
        loops_pg.write_projection(
            connection, schema, list(rebuilt.values()))
        after_states = {lid: view.state.value
                        for lid, view in rebuilt.items()}
        return {"ok": True, "schema": schema,
                "loops": len(rebuilt),
                "identical": before_states == after_states,
                "before": before_states, "after": after_states}
    finally:
        connection.close()


def do_loop_create(payload: dict[str, Any]) -> dict[str, Any]:
    """Explicit structured loop creation (testing / later actions)."""
    require_engine()
    from remembering.loops import postgres as loops_pg
    from remembering.loops.events import validate_transition_dict
    from remembering.loops.model import OpenLoopEvent

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    transition_raw = payload.get("transition")
    if not isinstance(transition_raw, dict):
        raise BridgeError("CONFIG_INVALID",
                          "loop-create requires a transition object")
    try:
        transition = validate_transition_dict({
            **transition_raw, "project_id": schema})
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
    import datetime as _dt
    import json as _json

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    custom_id = payload.get("event_id")
    if custom_id is not None and (
            not isinstance(custom_id, str) or not custom_id.strip()):
        raise BridgeError("CONFIG_INVALID",
                          "event_id must be a non-empty string")
    event = OpenLoopEvent(
        event_id=custom_id.strip() if isinstance(custom_id, str)
        else f"{transition.loop_id}-created",
        loop_id=transition.loop_id, project_id=schema,
        event_type="LOOP_CREATED", event_time=transition.created_at or now,
        recorded_at=now, evidence_refs=transition.evidence_refs,
        payload=tuple((k, v) for k, v in {
            "subject": transition.subject,
            "transition_kind": transition.transition_kind,
            "from_state": transition.from_state,
            "expected_state": transition.expected_state,
            "effective_from": transition.effective_from,
            "supersedes_loop_id": transition.supersedes_loop_id,
            "closure_kind": transition.closure_kind,
            "closure": _json.dumps(
                [r.to_dict() for r in transition.closure]),
        }.items()))
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                raise BridgeError(
                    "SCHEMA_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
        loops_pg.initialise(connection, schema)
        try:
            out = loops_pg.append_event(connection, schema, event, now)
        except ValueError as exc:
            raise BridgeError("CONFIG_INVALID", str(exc))
    finally:
        connection.close()
    return {"ok": True, "schema": schema, "loop_id": transition.loop_id,
            "event_id": event.event_id, "duplicate": out["duplicate"]}
    """Deterministic Stage 6 selection contract (no services)."""
    _ = payload
    require_engine()
    from remembering.select.evaluation import evaluate_selection

    report = evaluate_selection()
    return {"ok": bool(report["passed"]), **report}


def do_trace_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 7 trace contract (no services)."""
    _ = payload
    require_engine()
    from remembering.trace.evaluation import evaluate_trace

    report = evaluate_trace()
    return {"ok": bool(report["passed"]), **report}


def _trace_connection(schema: str, canonical_dir: str):
    connection = raw_connect()
    try:
        with connection.cursor() as cur:
            from psycopg import sql

            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.context_traces",))
            if cur.fetchone()[0] is None:
                connection.close()
                raise BridgeError(
                    "TRACE_STORE_MISSING",
                    "trace storage is not initialised; run memory_setup.")
            cur.execute(
                sql.SQL("SELECT value FROM {}.meta "
                        "WHERE key = 'project.path'").format(
                    sql.Identifier(schema)))
            row = cur.fetchone()
            if row is not None and row[0] != canonical_dir:
                connection.close()
                raise BridgeError(
                    "TRACE_PROJECT_MISMATCH",
                    f"schema {schema!r} is claimed by project {row[0]!r}.")
    except BridgeError:
        raise
    except Exception as exc:
        connection.close()
        raise BridgeError("TRACE_STORE_ERROR", str(exc))
    return connection


def do_trace(payload: dict[str, Any]) -> dict[str, Any]:
    """memory_trace: get | find | explain | verify | replay | diff."""
    require_engine()
    from remembering.trace import postgres as trace_pg
    from remembering.trace.query import (
        explain_candidate,
        find_traces,
        summarize,
    )
    from remembering.trace.replay import (
        diff_traces,
        integrity_replay,
        reconstruct_bundle,
    )

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    mode = payload.get("mode", "get")
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "get", "find", "explain", "verify", "replay", "diff"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown trace mode {payload.get('mode')!r}: expected 'get', "
            "'find', 'explain', 'verify', 'replay' or 'diff'.")
    mode = mode.strip().lower()

    connection = _trace_connection(schema, canonical_dir)
    try:
        if mode == "get":
            trace_id = payload.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise BridgeError("CONFIG_INVALID",
                                  "mode 'get' requires trace_id")
            stored = trace_pg.fetch_trace(connection, schema,
                                          trace_id.strip())
            if stored is None:
                return {"ok": False, "schema": schema,
                        "message": "TRACE_NOT_FOUND",
                        "trace": None}
            return {"ok": True, "schema": schema,
                    "trace_id": trace_id.strip(), "trace": stored,
                    "summary": summarize(stored)}
        if mode == "find":
            filters = payload.get("filters") or {}
            if not isinstance(filters, dict):
                raise BridgeError("CONFIG_INVALID",
                                  "filters must be an object")
            try:
                summaries = find_traces(connection, schema, filters)
            except ValueError as exc:
                raise BridgeError("CONFIG_INVALID", str(exc))
            return {"ok": True, "schema": schema, "count": len(summaries),
                    "traces": summaries}
        if mode == "explain":
            trace_id = payload.get("trace_id")
            candidate_id = payload.get("candidate_id")
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise BridgeError("CONFIG_INVALID",
                                  "mode 'explain' requires trace_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise BridgeError("CONFIG_INVALID",
                                  "mode 'explain' requires candidate_id")
            stored = trace_pg.fetch_trace(connection, schema,
                                          trace_id.strip())
            if stored is None:
                return {"ok": False, "schema": schema,
                        "message": "TRACE_NOT_FOUND", "explanation": None}
            return {"ok": True, "schema": schema,
                    "explanation": explain_candidate(
                        stored, candidate_id.strip())}
        if mode == "verify":
            trace_id = payload.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise BridgeError("CONFIG_INVALID",
                                  "mode 'verify' requires trace_id")
            stored = trace_pg.fetch_trace(connection, schema,
                                          trace_id.strip())
            if stored is None:
                return {"ok": False, "schema": schema,
                        "message": "TRACE_NOT_FOUND",
                        "verification": None}
            verification = integrity_replay(stored)
            rebuilt = reconstruct_bundle(stored)
            verification["reconstruction_matches"] = rebuilt["matches"]
            return {"ok": verification["ok"], "schema": schema,
                    "verification": verification}
        if mode == "replay":
            return _do_trace_replay(connection, schema, payload)
        result = _do_trace_diff(connection, schema, payload)
        return result
    finally:
        connection.close()


def _replay_policy(payload: dict, stored: dict) -> dict:
    """Resolve the replay trust policy: inline dict or 'current' file."""
    from remembering.trust.policy import (
        builtin_policy,
        load_trust_policy,
        validate_policy_dict,
    )

    spec = payload.get("policy")
    if spec is None or (isinstance(spec, str)
                        and spec.strip().lower() == "current"):
        return {"policy": None, "identity": {"source": "current-file"},
                "use_current_file": True}
    if not isinstance(spec, dict):
        raise BridgeError("CONFIG_INVALID",
                          "replay policy must be an object or 'current'")
    try:
        policy = validate_policy_dict(spec)
    except Exception as exc:
        raise BridgeError("CONFIG_INVALID", f"replay policy invalid: {exc}")
    _ = stored
    return {"policy": policy,
            "identity": {"version": policy.version,
                         "digest": policy.digest,
                         "source": "inline"},
            "use_current_file": False}


def _do_trace_replay(connection, schema: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
    from remembering.trace import postgres as trace_pg
    from remembering.trace.replay import (
        counterfactual_id,
        diff_traces,
        reconstruct_bundle,
    )
    from remembering.trust.gate import TrustContext, judge_all
    from remembering.trust.model import TrustCandidate
    from remembering.trust.policy import load_trust_policy
    from remembering.select.model import SelectionCandidate
    from remembering.select.policy import select as select_policy

    trace_id = payload.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "mode 'replay' requires trace_id")
    kind = payload.get("replay_kind", "trust")
    if not isinstance(kind, str) or kind.strip().lower() not in (
            "trust", "selection"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown replay_kind {payload.get('replay_kind')!r}: "
            "expected 'trust' or 'selection'.")
    kind = kind.strip().lower()
    stored = trace_pg.fetch_trace(connection, schema, trace_id.strip())
    if stored is None:
        return {"ok": False, "schema": schema, "message": "TRACE_NOT_FOUND",
                "replay": None}

    frozen = ["retrieval: frozen from source trace",
              "temporal: frozen", "frame: frozen"]
    if kind == "trust":
        replayed = _replay_trust(
            connection, schema, stored, payload, frozen)
    else:
        replayed = _replay_selection(stored, payload, frozen)
    if not replayed["ok"]:
        return {"ok": False, "schema": schema,
                "message": replayed["message"], "replay": None}
    diff = diff_traces(stored, replayed["pseudo_trace"])
    result = {
        "source_trace_id": trace_id.strip(),
        "replay_kind": kind,
        "evidence_boundary": frozen + replayed["boundary"],
        "verdicts": replayed["verdicts"],
        "selected_ids": replayed["selected_ids"],
        "bundle_digest": replayed["bundle_digest"],
        "diff": diff,
        "replay_id": counterfactual_id(
            stored.get("trace_id"), kind, replayed["policy_identity"],
            replayed["bundle_digest"]),
        "persisted": False,
    }
    if payload.get("persist") is True:
        pseudo = dict(replayed["pseudo_trace"])
        pseudo["execution_type"] = "counterfactual_replay"
        pseudo["source_trace_id"] = stored.get("trace_id")
        pseudo["replay_kind"] = kind
        try:
            from remembering.trace.postgres import insert_trace
            from remembering.trace.canonical import (
                bundle_digest_for,
                pool_digest_for,
            )
            entries = pseudo.get("candidate_pool", {}).get("entries", [])
            out = insert_trace(
                connection, schema, pseudo,
                pseudo.get("bundle", {}).get("content", ""), entries,
                pseudo.get("created_at", stored.get("created_at", "")),
                pseudo.get("candidates", []),
                [{"stage": "replay", "policy_name": kind,
                  "version": str(replayed["policy_identity"]),
                  "digest": str(replayed["policy_identity"])}])
            result["persisted"] = True
            result["replay_trace_id"] = out["trace_id"]
        except ValueError as exc:
            raise BridgeError("TRACE_STORE_ERROR", str(exc))
    return {"ok": True, "schema": schema, "replay": result}


def _replay_trust(connection, schema: str, stored: dict,
                  payload: dict, frozen: list) -> dict:
    from remembering.trust.gate import TrustContext, judge_all
    from remembering.trust.model import TrustCandidate
    from remembering.trust.policy import load_trust_policy

    resolved = _replay_policy(payload, stored)
    if resolved["use_current_file"]:
        from pathlib import Path as _Path

        project_dir = project_directory_from(payload)
        loaded = load_trust_policy(
            _Path(__import__("os").path.realpath(project_dir)))
        policy = loaded["policy"]
        identity = {"version": policy.version, "digest": policy.digest,
                    "source": policy.policy_source}
    else:
        policy = resolved["policy"]
        identity = resolved["identity"]
    entries = [c for c in stored.get("candidates", [])]
    if not entries:
        return {"ok": False,
                "message": "REPLAY_INPUT_INSUFFICIENT:candidates"}
    from remembering.trust.policy import match_rule as _replay_match
    from remembering.write.model import effective_class as _effective

    candidates: list[TrustCandidate] = []
    derived_map: dict[str, list[str]] = {}
    known: set[str] = set()
    replay_override: dict[str, str] = {}
    replay_ceilings: dict[str, str] = {}
    for entry in entries:
        trust_input = entry.get("trust_input", {})
        ceiling = trust_input.get("write_ceiling")
        if ceiling is not None:
            rule, _ = _replay_match(policy, entry["source_id"])
            policy_class = (rule.source_class if rule is not None
                            else policy.default_source_class)
            eff = _effective(policy_class, ceiling)
            replay_override[entry["source_id"]] = eff
            replay_ceilings[entry["source_id"]] = ceiling
            entry_class = eff
        else:
            entry_class = trust_input.get("source_class",
                                          "informational")
        candidates.append(TrustCandidate(
            unit_id=entry["candidate_id"],
            source_id=entry["source_id"],
            project_id=stored.get("project", {}).get("project_id", ""),
            text=entry.get("snapshot_text", ""),
            source_class=entry_class,
            role=trust_input.get("role", "ordinary"),
            temporal_status=(entry.get("temporal") or {}).get(
                "status", "not_modelled"),
            frame_control="query_only",
            claim_key=trust_input.get("claim_key", ""),
            derived_from=tuple(trust_input.get("derived_from", [])),
            refuted_by=tuple(trust_input.get("refuted_by", [])),
            restricted_to=tuple(trust_input.get("restricted_to", []))))
        derived_map[entry["source_id"]] = list(
            trust_input.get("derived_from", []))
        known.add(entry["source_id"])
    caller = ((payload.get("trust") or {}).get("caller_scope")
              if isinstance(payload.get("trust"), dict) else None)
    caller = caller or (stored.get("stages", {}).get("trust") or {}).get(
        "caller_scope", "default")
    ctx = TrustContext(
        policy=policy, revoked=set(
            (stored.get("stages", {}).get("trust") or {}).get(
                "revoked_sources", [])),
        restricted=(stored.get("stages", {}).get("trust") or {}).get(
            "restricted_sources", {}),
        derived_from=derived_map, known_sources=known,
        project_id=stored.get("project", {}).get("project_id", ""),
        caller_scope=caller, level="FULL",
        standing_override=replay_override,
        write_ceiling=replay_ceilings)
    records = judge_all(candidates, ctx)
    verdicts = {r.unit_id: {"verdict": r.verdict.value,
                            "reason": r.reason, "stage": r.stage}
                for r in records}
    admitted = [r.unit_id for r in records
                if r.verdict.value == "admit"]
    pseudo = _pseudo_trace_for_replay(
        stored, verdicts, admitted, "trust", identity, frozen)
    return {"ok": True, "verdicts": verdicts, "selected_ids": admitted,
            "bundle_digest": pseudo["bundle"]["bundle_digest"],
            "pseudo_trace": pseudo, "policy_identity": identity,
            "boundary": ["trust: replayed", "selection: rerun downstream"]}


def _replay_selection(stored: dict, payload: dict,
                      frozen: list) -> dict:
    from remembering.select.model import SelectionCandidate
    from remembering.select.policy import select as select_policy

    mode = payload.get("selection_mode", "full")
    if not isinstance(mode, str) or mode.strip().lower() not in (
            "decisive", "full"):
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown selection_mode {payload.get('selection_mode')!r}: "
            "expected 'decisive' or 'full'.")
    mode = mode.strip().lower()
    admitted = [c for c in stored.get("candidates", [])
                if c.get("trust", {}).get("verdict") == "admit"
                or c.get("final_selected")]
    if not admitted:
        return {"ok": False,
                "message": "REPLAY_INPUT_INSUFFICIENT:admitted pool"}
    candidates: list[SelectionCandidate] = []
    derived_map: dict[str, list[str]] = {}
    for entry in admitted:
        trust_input = entry.get("trust_input", {})
        retrieval = entry.get("retrieval", {})
        selection = entry.get("selection", {})
        candidates.append(SelectionCandidate(
            chunk_id=entry["candidate_id"],
            source_id=entry["source_id"],
            text=entry.get("snapshot_text", ""),
            fused_rank=retrieval.get("fused_rank") or 0,
            lexical_rank=retrieval.get("lexical_rank"),
            dense_rank=retrieval.get("dense_rank"),
            score=float(retrieval.get("score") or 0.0),
            from_query="lexical" in (retrieval.get("paths", []) or []),
            from_objective="objective" in (retrieval.get("paths", [])
                                           or []),
            temporal_status=(entry.get("temporal") or {}).get(
                "status", "not_modelled"),
            frame_control="query_only",
            evidence_class="prose",
            frame_preferred=False,
            trust_reason=(entry.get("trust") or {}).get("reason", ""),
            source_class=trust_input.get("source_class",
                                         "informational"),
            role=trust_input.get("role", "ordinary"),
            claim_key=trust_input.get("claim_key", ""),
            derived_from=tuple(trust_input.get("derived_from", [])),
            dispute_key="",
            negative=False,
            constraint_hit=False))
        derived_map[entry["source_id"]] = list(
            trust_input.get("derived_from", []))
    stored_selection = stored.get("stages", {}).get("selection", {})
    result = select_policy(
        candidates, derived_map,
        max_chars=(stored.get("request", {}) or {}).get("max_chars", 4000),
        max_results=(stored.get("request", {}) or {}).get("max_results", 6),
        mode=mode)
    selected_ids = [r.chunk_id for r in result.selected]
    verdicts = {r.chunk_id: {"disposition": r.disposition.value,
                             "reason": r.reason}
                for r in (*result.selected, *result.dropped)}
    pseudo = _pseudo_trace_for_replay(
        stored, verdicts, selected_ids, "selection",
        {"mode": mode,
         "policy_version": stored_selection.get("policy_version")},
        frozen)
    return {"ok": True, "verdicts": verdicts,
            "selected_ids": selected_ids,
            "bundle_digest": pseudo["bundle"]["bundle_digest"],
            "pseudo_trace": pseudo,
            "policy_identity": {"mode": mode},
            "boundary": ["selection: replayed"]}


def _pseudo_trace_for_replay(stored: dict, verdicts: dict,
                             selected_ids: list, kind: str,
                             policy_identity: dict, frozen: list) -> dict:
    from remembering.trace.canonical import bundle_digest_for

    order = [c["candidate_id"] for c in stored.get("candidates", [])
             if c["candidate_id"] in set(selected_ids)]
    lines = []
    for cid in order:
        candidate = next(c for c in stored.get("candidates", [])
                         if c["candidate_id"] == cid)
        lines.append(f"[source: {candidate.get('source_id')} | "
                     f"chunk: {cid}]\n{candidate.get('snapshot_text', '')}")
    content = "\n\n---\n\n".join(lines)
    pseudo = {
        "schema_version": stored.get("schema_version"),
        "project": stored.get("project", {}),
        "created_at": stored.get("created_at"),
        "request": stored.get("request", {}),
        "route": stored.get("route", {}),
        "versions": stored.get("versions", {}),
        "stages": stored.get("stages", {}),
        "candidates": stored.get("candidates", []),
        "candidate_pool": stored.get("candidate_pool", {}),
        "bundle": {"content": content, "chars": len(content),
                   "render_order": order,
                   "bundle_digest": bundle_digest_for(content)},
        "funnel": stored.get("funnel", {}),
        "timings": {},
        "input_digest": stored.get("input_digest"),
    }
    return pseudo


def _do_trace_diff(connection, schema: str,
                   payload: dict[str, Any]) -> dict[str, Any]:
    from remembering.trace import postgres as trace_pg
    from remembering.trace.replay import diff_traces

    trace_id = payload.get("trace_id")
    other_id = payload.get("diff_with")
    if not isinstance(trace_id, str) or not trace_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "mode 'diff' requires trace_id")
    if not isinstance(other_id, str) or not other_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "mode 'diff' requires diff_with")
    old = trace_pg.fetch_trace(connection, schema, trace_id.strip())
    new = trace_pg.fetch_trace(connection, schema, other_id.strip())
    if old is None or new is None:
        return {"ok": False, "schema": schema,
                "message": "TRACE_NOT_FOUND", "diff": None}
    return {"ok": True, "schema": schema,
            "diff": diff_traces(old, new)}


def do_route_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic routing contract evaluation (no model calls)."""
    _ = payload
    require_engine()
    from remembering.baseline.routing import evaluate_router

    report = evaluate_router()
    return {"ok": bool(report["passed"]), **report}


ADMISSION_NOTE = (
    "Retrieval evidence only. Later temporal, frame and trust admission "
    "stages decide whether any of this evidence may influence present "
    "action; superseded history may still be valid for historical recall."
)


# -- canonical session capture -------------------------------------------------
#
# Session transcripts are canonical history, not derived memory. The hook
# merges unseen OpenCode messages into
#   <project>/.remembering/sessions/<session_id>.json
# keyed by message id (or a content fingerprint when no id is present).
# Old events are never rewritten; duplicates are never appended. The next
# memory_setup/memory_refresh ingests changed transcripts as ordinary
# versioned sources through the standard Baseline.refresh path.

SESSIONS_DIRNAME = ".remembering"
SESSIONS_SUBDIR = "sessions"


def sessions_dir(project_dir: Path) -> Path:
    return (Path(os.path.realpath(project_dir)) / SESSIONS_DIRNAME
            / SESSIONS_SUBDIR)


def fingerprint_message(message: dict[str, Any]) -> str:
    material = json.dumps(
        {"role": message.get("role", ""),
         "text": message.get("text", ""),
         "tools": [message.get("tool_calls"), message.get("tool_results")]},
        sort_keys=True, default=str,
    )
    return "msg:" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def do_capture_session(payload: dict[str, Any]) -> dict[str, Any]:
    # Capture needs no database: canonical history must survive even when
    # PostgreSQL is down. It only needs the project checkout.
    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or \
            not SESSION_ID_PATTERN.match(session_id):
        raise BridgeError(
            "CONFIG_INVALID",
            "session_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,127}; "
            "refusing to write outside the sessions directory.",
        )
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise BridgeError("CONFIG_INVALID", "messages must be a list")

    target_dir = sessions_dir(project_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = target_dir / f"{session_id}.json"

    if transcript_path.exists():
        try:
            existing = json.loads(transcript_path.read_text(
                encoding="utf-8"))
        except Exception as exc:
            raise BridgeError(
                "CAPTURE_CONFLICT",
                f"existing transcript {transcript_path} is not valid JSON "
                f"({type(exc).__name__}); refusing to overwrite canonical "
                "history.",
            )
        if not isinstance(existing, dict) or not isinstance(
                existing.get("events"), list):
            raise BridgeError(
                "CAPTURE_CONFLICT",
                f"existing transcript {transcript_path} has an unexpected "
                "shape; refusing to overwrite canonical history.",
            )
    else:
        existing = {
            "kind": "opencode-session-transcript",
            "version": 1,
            "session_id": session_id,
            "agent": payload.get("agent"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "events": [],
        }

    seen: set[str] = set()
    for event in existing["events"]:
        if isinstance(event, dict):
            if isinstance(event.get("id"), str) and event["id"]:
                seen.add(f"id:{event['id']}")
            elif isinstance(event.get("fingerprint"), str):
                seen.add(event["fingerprint"])

    recorded = 0
    duplicates = 0
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "") or "")
        text = str(message.get("text", "") or "")
        if not role and not text:
            continue
        key = None
        mid = message.get("id")
        if isinstance(mid, str) and mid:
            key = f"id:{mid}"
        if key is None:
            key = fingerprint_message({"role": role, "text": text,
                                       "tool_calls": message.get(
                                           "tool_calls"),
                                       "tool_results": message.get(
                                           "tool_results")})
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        existing["events"].append({
            "id": mid if isinstance(mid, str) and mid else None,
            "fingerprint": key if key.startswith("msg:") else None,
            "role": role,
            "text": text,
            "agent": message.get("agent"),
            "model": message.get("model"),
            "tool_calls": message.get("tool_calls"),
            "tool_results": message.get("tool_results"),
            "observed_at": message.get("observed_at") or stamp,
        })
        recorded += 1

    for field in ("agent", "provider", "model"):
        if existing.get(field) is None and payload.get(field) is not None:
            existing[field] = payload[field]

    tmp_path = transcript_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    os.replace(tmp_path, transcript_path)

    return {
        "ok": True,
        "schema": schema,
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "recorded": recorded,
        "duplicates": duplicates,
        "total": len(existing["events"]),
    }


# -- entry ---------------------------------------------------------------------

# -- explicit memory actions (Stage 9) --------------------------------------
#
# A memory write is an event, not an edit to the past. REMEMBER appends
# one immutable record; CORRECT/SUPERSEDE append one record plus a
# lifecycle relationship; RETRACT appends a relationship/event without
# replacement content. Every write carries runtime attribution and the
# write-policy identity, becomes an ordinary indexed source under
# memory://explicit/, and later passes the same temporal/frame/trust/
# selection/trace controls as everything else. WRITE_ALLOWED is never
# TRUST_ADMITTED: the write policy assigns at most a standing ceiling
# and Stage 5 remains the final trust authority.

WRITE_ORIGINS = ("opencode", "cli", "import")

MEMORY_EVENTS_REL = Path(".remembering") / "memory" / "events.jsonl"


def parse_remember_spec(payload: dict[str, Any]) -> tuple[dict, dict]:
    """Split a remember payload into the engine request and runtime
    attribution. Project/actor/surface identity comes from the host
    adapter here, never from caller-set trust fields (the engine
    rejects those)."""
    if not isinstance(payload, dict):
        raise BridgeError("CONFIG_INVALID",
                          "remember payload must be an object")
    raw: dict[str, Any] = {
        "action": payload.get("action", "remember"),
        "content": payload.get("content"),
        "target_record_id": payload.get("target_record_id"),
        "role": payload.get("role", "ordinary"),
        "reason": payload.get("reason"),
        "evidence_refs": payload.get("evidence_refs", []),
        "effective_from": payload.get("effective_from"),
        "event_time": payload.get("event_time"),
        "idempotency_key": payload.get("idempotency_key"),
    }
    origin = payload.get("origin", "opencode")
    if origin is None:
        origin = "opencode"
    if not isinstance(origin, str) or origin.strip() not in \
            WRITE_ORIGINS:
        raise BridgeError(
            "CONFIG_INVALID",
            f"unknown remember origin {payload.get('origin')!r}: "
            "expected 'opencode', 'cli' or 'import'.")
    origin = origin.strip()
    scope = payload.get("caller_scope", "default")
    if scope is None:
        scope = "default"
    if not isinstance(scope, str) or not scope.strip():
        raise BridgeError("CONFIG_INVALID",
                          "remember caller_scope must be a non-empty "
                          "string")
    actor_kind = payload.get("actor_kind")
    if actor_kind is None:
        actor_kind = {"opencode": "agent", "cli": "human",
                      "import": "import"}[origin]
    if not isinstance(actor_kind, str) or not actor_kind.strip():
        raise BridgeError("CONFIG_INVALID",
                          "remember actor_kind must be a non-empty "
                          "string")
    actor_id = payload.get("actor_id", "")
    if actor_id is None:
        actor_id = ""
    if not isinstance(actor_id, str):
        raise BridgeError("CONFIG_INVALID",
                          "remember actor_id must be a string")
    host_session = payload.get("host_session_id", "")
    host_tool_call = payload.get("host_tool_call_id", "")
    for label, value in (("host_session_id", host_session),
                         ("host_tool_call_id", host_tool_call)):
        if not isinstance(value, str):
            raise BridgeError("CONFIG_INVALID",
                              f"remember {label} must be a string")
    attribution = {
        "surface": origin,
        "caller_scope": scope.strip(),
        "actor_kind": actor_kind.strip(),
        "actor_id": actor_id.strip(),
        "host_session_id": host_session.strip(),
        "host_tool_call_id": host_tool_call.strip(),
        "idempotency_key": None,
    }
    return raw, attribution


def validate_write_config(project_dir: Path) -> dict[str, Any]:
    """Validate the explicit write policy if present. Absence yields
    the conservative builtin (healthy); malformed files fail."""
    from remembering.write.policy import load_write_policy

    loaded = load_write_policy(Path(os.path.realpath(project_dir)))
    if not loaded["valid"]:
        return {"error": loaded["error"], "detail": "invalid"}
    if not loaded["configured"]:
        return {"error": None, "detail": "builtin default"}
    policy = loaded["policy"]
    return {"error": None,
            "detail": f"explicit v{policy.version}"}


def write_health(connection, schema: str,
                 project_dir: Path) -> dict[str, Any]:
    """Write section for memory_health. Zero records is healthy;
    builtin-default policy is healthy; malformed policy is not."""
    from remembering.write import (
        EXPLICIT_RECORD_SCHEMA,
        MEMORY_ACTION_SCHEMA,
        RELATION_VERSION,
        WRITE_ENGINE_VERSION,
        WRITE_STORE_VERSION,
    )
    from remembering.write import postgres as write_pg
    from remembering.write.policy import load_write_policy

    section: dict[str, Any] = {
        "write_engine_version": WRITE_ENGINE_VERSION,
        "store_version": WRITE_STORE_VERSION,
        "record_schema_version": EXPLICIT_RECORD_SCHEMA,
        "action_schema_version": MEMORY_ACTION_SCHEMA,
        "relation_version": RELATION_VERSION,
        "ready": False,
        "policy": {
            "configured": False,
            "valid": True,
            "version": None,
            "digest": None,
            "source": "builtin_default",
            "error": None,
        },
        "record_count": 0,
        "action_count": 0,
        "remember_count": 0,
        "correct_count": 0,
        "supersede_count": 0,
        "retract_count": 0,
        "relationship_count": 0,
        "unresolved_index_records": [],
        "last_action_at": None,
    }
    loaded = load_write_policy(Path(os.path.realpath(project_dir)))
    section["policy"] = {
        "configured": loaded["configured"],
        "valid": loaded["valid"],
        "version": loaded["policy"].version,
        "digest": loaded["policy"].digest,
        "source": loaded["policy"].policy_source,
        "error": loaded["error"],
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)",
                        (f"{schema}.explicit_memory_records",))
            if cur.fetchone()[0] is None:
                return section
        section["ready"] = True
        tallies = write_pg.counts(connection, schema)
        section["record_count"] = tallies["records"]
        section["action_count"] = tallies["actions"]
        section["remember_count"] = tallies["remember"]
        section["correct_count"] = tallies["correct"]
        section["supersede_count"] = tallies["supersede"]
        section["retract_count"] = tallies["retract"]
        section["relationship_count"] = tallies["relations"]
        section["last_action_at"] = tallies["last_action_at"]
        section["unresolved_index_records"] = \
            write_pg.unresolved_index_records(connection, schema)
    except Exception as exc:
        section["error"] = (
            f"{type(exc).__name__}: {str(exc)[:160]}")
    return section


def _write_pg_deps(read_conn, schema: str, project_dir: Path,
                   canonical_dir: str, embedder,
                   project_id: str) -> dict:
    """Service dependencies backed by PostgreSQL. Reads ride the
    shared autocommit connection; each accepted action commits
    through its own transaction (see _write_transact)."""
    from remembering.temporal import postgres as temporal_pg
    from remembering.write import postgres as write_pg
    from remembering.write.policy import load_write_policy

    def load_policy() -> dict:
        return load_write_policy(Path(os.path.realpath(project_dir)))

    def evidence_exists(ref: str) -> bool:
        if write_pg.get_record(read_conn, schema, ref) is not None:
            return True
        bare = ref
        if bare.startswith("memory://explicit/"):
            bare = bare[len("memory://explicit/"):]
            if write_pg.get_record(read_conn, schema, bare) \
                    is not None:
                return True
        from psycopg import sql as _sql

        try:
            with read_conn.cursor() as cur:
                cur.execute(
                    _sql.SQL("SELECT 1 FROM {}.sources WHERE "
                             "source_id = %s").format(
                        _sql.Identifier(schema)), (ref,))
                if cur.fetchone() is not None:
                    return True
                cur.execute(
                    _sql.SQL("SELECT 1 FROM {}.chunks WHERE "
                             "chunk_id = %s").format(
                        _sql.Identifier(schema)), (ref,))
                return cur.fetchone() is not None
        except Exception:
            return False

    def next_source_seq(source_id: str) -> int:
        from psycopg import sql as _sql

        with read_conn.cursor() as cur:
            cur.execute(
                _sql.SQL("SELECT COALESCE(MAX(source_seq), 0) + 1 "
                         "FROM {}.temporal_events WHERE source_id = "
                         "%s").format(_sql.Identifier(schema)),
                (source_id,))
            row = cur.fetchone()
            return int(row[0]) if row is not None else 1

    def embed(texts: list[str]) -> dict:
        produced = embedder.embed(texts)
        return {"vectors": [list(v) for v in produced.vectors],
                "version": embedder.version()}

    return {
        "load_policy": load_policy,
        "now": _write_now,
        "get_record": lambda rid: write_pg.get_record(
            read_conn, schema, rid),
        "successors": lambda rid: write_pg.successors_of(
            read_conn, schema, rid),
        "get_action": lambda aid: write_pg.get_action(
            read_conn, schema, aid),
        "get_action_by_key": lambda key:
        write_pg.get_action_by_idempotency(read_conn, schema, key),
        "evidence_exists": evidence_exists,
        "next_source_seq": next_source_seq,
        "embed": embed,
        "transact": lambda body: _write_transact(schema, body),
    }


def _write_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_transact(schema: str, body) -> None:
    """One accepted action, one transaction: canonical record,
    action event, relationship projection, temporal overlay, and
    retrieval rows commit together or not at all."""
    import psycopg

    from remembering.temporal import postgres as temporal_pg
    from remembering.write import postgres as write_pg

    conn = psycopg.connect(dsn(), autocommit=False)
    try:
        def put_record(record) -> None:
            write_pg.insert_record(conn, schema, record)

        def put_action(event, received_at: str) -> None:
            write_pg.insert_action(conn, schema, event,
                                   received_at)

        def put_relation(source: str, target: str, relation: str,
                         action_id: str, effective,
                         recorded) -> None:
            write_pg.insert_relation(conn, schema, source, target,
                                     relation, action_id, effective,
                                     recorded)

        def put_temporal(envelope) -> None:
            temporal_pg.append_event(conn, schema, envelope,
                                     envelope.recorded_at)

        def put_chunks(source_row, chunk_rows) -> None:
            _put_explicit_chunks(conn, schema, source_row,
                                 chunk_rows)

        body({"put_record": put_record, "put_action": put_action,
              "put_relation": put_relation,
              "put_temporal": put_temporal,
              "put_chunks": put_chunks})
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.commit()
    conn.close()


def _put_explicit_chunks(conn, schema: str, source_row,
                         chunk_rows) -> None:
    """Index one explicit record as an ordinary retrieval source
    (same table, same FTS/vector pipeline, stable memory://explicit/
    namespace). Runs inside the action transaction."""
    from psycopg import sql as _sql

    source_id, artifact_type, content_hash, timestamp = source_row
    with conn.cursor() as cur:
        cur.execute(
            _sql.SQL(
                "INSERT INTO {}.sources (source_id, artifact_type, "
                "content_hash, timestamp) VALUES (%s, %s, %s, %s)"
            ).format(_sql.Identifier(schema)),
            (source_id, artifact_type, content_hash, timestamp),
        )
        for (chunk_id, chunk_source, ordinal, text, section,
             char_start, char_end, chunk_hash, chunker,
             emb_version, vector) in chunk_rows:
            cur.execute(
                _sql.SQL(
                    "INSERT INTO {}.chunks (chunk_id, source_id, "
                    "ordinal, text, section, char_start, char_end, "
                    "content_hash, chunker, embedding_version, "
                    "embedding, tsv) VALUES (%s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, "
                    "to_tsvector('english', %s))"
                ).format(_sql.Identifier(schema)),
                (chunk_id, chunk_source, ordinal, text, section,
                 char_start, char_end, chunk_hash, chunker,
                 emb_version, list(vector), text),
            )


def _ensure_write_ready(schema: str, project_dir: Path,
                        canonical_dir: str) -> None:
    """Fail closed before any write: schema initialised, project
    identity intact, write tables present."""
    if not schema_initialized(schema):
        raise BridgeError(
            "WRITE_STORE_NOT_READY",
            "this project's remembering schema is not initialised; "
            "run memory_setup before memory_remember.")
    connection = raw_connect()
    try:
        from psycopg import sql as _sql

        from remembering.write import postgres as write_pg

        with connection.cursor() as cur:
            cur.execute("SELECT key, value FROM {}.meta".format(
                f'"{schema}"'))
            meta = {row[0]: row[1] for row in cur.fetchall()}
        recorded = meta.get("project.path")
        if recorded is not None and recorded != canonical_dir:
            raise BridgeError(
                "SCHEMA_MISMATCH",
                f"schema {schema!r} is already claimed by project "
                f"{recorded!r}, but this project resolves to "
                f"{canonical_dir!r}. Refusing to mix projects.")
        ensure_temporal_objects(connection, schema)
        write_pg.initialise(connection, schema)
    finally:
        connection.close()


def do_remember(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.write.service import perform_write

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    emb = embedding_spec_from(payload)
    raw, attribution = parse_remember_spec(payload)
    attribution = {**attribution, "project_id": schema}
    if payload.get("idempotency_key") and \
            not raw.get("idempotency_key"):
        raw = {**raw,
               "idempotency_key": payload["idempotency_key"]}
    _ensure_write_ready(schema, project_dir, canonical_dir)
    check_embedding(emb)
    embedder = build_embedder(emb)
    connection = raw_connect()
    try:
        deps = _write_pg_deps(connection, schema, project_dir,
                              canonical_dir, embedder, schema)
        result = perform_write(deps, raw, attribution)
    finally:
        connection.close()
    result["schema"] = schema
    return result


def import_memory_events(connection, schema: str,
                         project_dir: Path, embedder) -> dict:
    """Deterministic import of operator-authored memory actions from
    .remembering/memory/events.jsonl. Every line still passes schema
    validation, project isolation, write authorization for the import
    surface, and relationship validation: import is never an
    authority bypass. Exact replays are idempotent duplicates."""
    from remembering.write.service import perform_write

    report: dict[str, Any] = {"discovered": 0, "authorized": 0,
                              "imported": 0, "duplicates": 0,
                              "denied": [], "failed": []}
    path = Path(os.path.realpath(project_dir)) / MEMORY_EVENTS_REL
    if not path.is_file():
        return report
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report["failed"].append(f"{path}: unreadable: {exc}")
        return report
    canonical_dir = canonical_project_dir(project_dir)
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        report["discovered"] += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            report["failed"].append(f"line {lineno}: not JSON: {exc}")
            continue
        if not isinstance(entry, dict):
            report["failed"].append(
                f"line {lineno}: must be an object")
            continue
        raw = {
            "action": entry.get("action", "remember"),
            "content": entry.get("content"),
            "target_record_id": entry.get("target_record_id"),
            "role": entry.get("role", "ordinary"),
            "reason": entry.get("reason"),
            "evidence_refs": entry.get("evidence_refs", []),
            "effective_from": entry.get("effective_from"),
            "event_time": entry.get("event_time"),
            "idempotency_key": entry.get("idempotency_key"),
        }
        scope = entry.get("caller_scope", "import")
        if not isinstance(scope, str) or not scope.strip():
            report["failed"].append(
                f"line {lineno}: caller_scope must be a string")
            continue
        actor_id = entry.get("actor_id", "")
        if not isinstance(actor_id, str):
            report["failed"].append(
                f"line {lineno}: actor_id must be a string")
            continue
        attribution = {
            "project_id": schema,
            "surface": "import",
            "caller_scope": scope.strip(),
            "actor_kind": "import",
            "actor_id": actor_id.strip(),
            "host_session_id": "",
            "host_tool_call_id": "",
            "idempotency_key": None,
        }
        try:
            deps = _write_pg_deps(connection, schema, project_dir,
                                  canonical_dir, embedder, schema)
            out = perform_write(deps, raw, attribution)
        except Exception as exc:
            report["failed"].append(
                f"line {lineno}: {type(exc).__name__}:"
                f"{str(exc)[:160]}")
            continue
        if out.get("ok"):
            report["authorized"] += 1
            if out.get("duplicate"):
                report["duplicates"] += 1
            else:
                report["imported"] += 1
        else:
            report["denied"].append(
                f"line {lineno}: {out.get('code')}:"
                f"{out.get('reason')}")
    return report


def annotate_explicit_metadata(items: list[dict],
                               schema: str) -> None:
    """Search stays broad: explicit-memory results gain structured
    annotations (role, ceiling, attribution, lifecycle successors).
    Nothing is hidden; failures degrade to unannotated items."""
    try:
        from remembering.write import postgres as write_pg

        sources = [i["source_id"] for i in items
                   if isinstance(i.get("source_id"), str)
                   and i["source_id"].startswith(
                       "memory://explicit/")]
        if not sources:
            return
        connection = raw_connect()
        try:
            attributes = write_pg.source_attributes(
                connection, schema, sources)
            relations = write_pg.all_relations(connection, schema)
        finally:
            connection.close()
        successors: dict[str, list] = {}
        for relation in relations:
            successors.setdefault(
                relation["target_record_id"], []).append({
                    "record_id": relation["source_record_id"],
                    "relation": relation["relation"],
                    "action_id": relation["action_id"],
                    "effective_from": relation["effective_from"],
                    "recorded_at": relation["recorded_at"]})
        for item in items:
            attrs = attributes.get(item["source_id"])
            if attrs is None:
                continue
            item["explicit_memory"] = {
                "record_id": attrs["record_id"],
                "role": attrs["role"],
                "standing_ceiling": attrs["standing_ceiling"],
                "actor_kind": None,
                "lineage_root": attrs["lineage_root"],
                "created_by_action": attrs["created_by_action"],
                "successors": successors.get(attrs["record_id"],
                                             []),
            }
    except Exception:
        pass


def load_write_overlay(schema: str) -> dict[str, Any]:
    """Canonical explicit records plus derived relations for the
    read path. Missing tables mean no explicit memory yet: empty
    overlay, never a failure."""
    from remembering.write import postgres as write_pg

    try:
        connection = raw_connect()
        try:
            records = {r.record_id: r for r in
                       write_pg.all_records(connection, schema)}
            relations = write_pg.all_relations(connection, schema)
        finally:
            connection.close()
    except Exception:
        records, relations = {}, []
    return {"records": records, "relations": relations}


def do_write_eval(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.write.evaluation import evaluate_write

    report = evaluate_write()
    out = {"ok": report["passed"], "schema": "",
           "contract_categories": report["contract_categories"],
           "eval_version": report["eval_version"],
           "checks_total": report["checks_total"],
           "checks_passed": report["checks_passed"],
           "categories": report["categories"],
           "passed": report["passed"]}
    try:
        out["schema"] = schema_from(payload)
    except BridgeError:
        pass
    return out


def do_write_rebuild(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild derived write projections from canonical records and
    actions: retrieval rows, relationship edges, and the temporal
    overlay. Canonical IDs never change; the rebuild reports
    equivalence or fails visibly."""
    require_engine()
    import hashlib as _hashlib

    from remembering.temporal import postgres as temporal_pg
    from remembering.temporal.reducer import projection_digest, replay
    from remembering.temporal.ordering import temporal_sort
    from remembering.write import postgres as write_pg
    from remembering.write.service import chunk_id_for
    from remembering.write.temporal import envelope_for_action

    schema = schema_from(payload)
    project_dir = project_directory_from(payload)
    canonical_dir = canonical_project_dir(project_dir)
    emb = embedding_spec_from(payload)
    _ensure_write_ready(schema, project_dir, canonical_dir)
    check_embedding(emb)
    embedder = build_embedder(emb)
    probe = embedder.embed(["dimension probe"])
    connection = raw_connect()
    try:
        records = write_pg.all_records(connection, schema)
        actions = sorted(
            _write_all_actions(connection, schema),
            key=lambda e: (e.recorded_at, e.action_id))
        log_before = temporal_pg.load_log(connection, schema)
        digest_before = projection_digest(
            replay(temporal_sort(log_before.events(),
                                 log_before.by_id())))
        record_ids_before = sorted(r.record_id for r in records)
        action_ids_before = sorted(e.action_id for e in actions)
        relation_ids_before = sorted(
            (r["source_record_id"], r["target_record_id"],
             r["relation"]) for r in
            write_pg.all_relations(connection, schema))
    finally:
        connection.close()
    # External embedding first; the destructive step stays inside
    # one transaction afterwards.
    vectors: dict[str, list[float]] = {}
    for record in records:
        produced = embedder.embed([record.content])
        if not produced.vectors or not produced.vectors[0]:
            raise BridgeError("WRITE_INDEX_FAILED",
                              "rebuild embedding produced no vector; "
                              "derived projections untouched.")
        vectors[record.record_id] = list(produced.vectors[0])
    emb_version = embedder.version()
    by_action = {e.action_id: e for e in actions}
    tx = raw_connect()
    old_autocommit = tx.autocommit
    tx.autocommit = False
    try:
        from psycopg import sql as _sql

        with tx.cursor() as cur:
            cur.execute(
                _sql.SQL("DELETE FROM "
                         "{}.explicit_memory_relations").format(
                    _sql.Identifier(schema)))
            cur.execute(
                _sql.SQL("DELETE FROM {}.chunks WHERE source_id LIKE "
                         "'memory://explicit/%'").format(
                    _sql.Identifier(schema)))
            cur.execute(
                _sql.SQL("DELETE FROM {}.sources WHERE source_id LIKE "
                         "'memory://explicit/%'").format(
                    _sql.Identifier(schema)))
            cur.execute(
                _sql.SQL("DELETE FROM {}.temporal_events WHERE "
                         "subject LIKE 'explicit-memory:%' OR "
                         "source_id LIKE 'memory://explicit/%'"
                         ).format(_sql.Identifier(schema)))
        derived = write_pg.derive_relations(
            [e.to_dict() for e in actions])
        for item in derived:
            write_pg.insert_relation(
                tx, schema, item["source_record_id"],
                item["target_record_id"], item["relation"],
                item["action_id"], item["effective_from"],
                item["recorded_at"])
        seq_by_source: dict[str, int] = {}
        for record in sorted(records,
                             key=lambda r: (r.recorded_at,
                                            r.record_id)):
            chunk_id = chunk_id_for(record.source_id,
                                    record.content)
            _put_explicit_chunks(
                tx, schema,
                (record.source_id, "explicit-memory",
                 record.content_hash, record.recorded_at),
                [(chunk_id, record.source_id, 0, record.content,
                  None, 0, len(record.content),
                  record.content_hash, "explicit-memory-v0.1",
                  emb_version, vectors[record.record_id])])
        for event in actions:
            record = next(
                (r for r in records
                 if r.record_id == event.new_record_id), None)
            lineage = (record.lineage_root if record is not None
                       else "")
            if not lineage and event.target_record_id:
                target = next(
                    (r for r in records
                     if r.record_id == event.target_record_id),
                    None)
                lineage = target.lineage_root if target is not None \
                    else ""
            prior = None
            if event.relation in ("corrects", "supersedes") and \
                    event.target_record_id:
                target = next(
                    (r for r in records
                     if r.record_id == event.target_record_id),
                    None)
                if target is not None:
                    creating = by_action.get(
                        target.created_by_action)
                    from remembering.write.service import \
                        target_event_id as _target_event

                    prior = _target_event(target, creating)
            source_id = (record.source_id if record is not None
                         else f"memory://explicit/"
                         f"{event.target_record_id or ''}")
            seq_by_source[source_id] = seq_by_source.get(
                source_id, 0) + 1
            envelope = envelope_for_action(
                record, event, lineage, prior,
                seq_by_source[source_id])
            temporal_pg.append_event(tx, schema, envelope,
                                     envelope.recorded_at)
        tx.commit()
    except Exception:
        tx.rollback()
        tx.close()
        raise
    tx.close()
    connection = raw_connect()
    try:
        records_after = write_pg.all_records(connection, schema)
        log_after = temporal_pg.load_log(connection, schema)
        digest_after = projection_digest(
            replay(temporal_sort(log_after.events(),
                                 log_after.by_id())))
        equivalent = (
            sorted(r.record_id for r in records_after) ==
            record_ids_before
            and digest_before == digest_after
            and sorted(
                (r["source_record_id"], r["target_record_id"],
                 r["relation"]) for r in
                write_pg.all_relations(connection, schema)) ==
            relation_ids_before)
    finally:
        connection.close()
    return {"ok": equivalent, "schema": schema,
            "records": len(record_ids_before),
            "actions": len(action_ids_before),
            "relations": len(relation_ids_before),
            "projection_digest": digest_after,
            "equivalent": equivalent,
            "embedding": emb_version,
            "message": ("rebuild equivalent"
                        if equivalent else
                        "REBUILD_DIVERGED: derived state differs "
                        "from canonical history")}


def _write_all_actions(connection, schema: str) -> list:
    from psycopg import sql as _sql

    from remembering.write import postgres as write_pg

    with connection.cursor() as cur:
        cur.execute(
            _sql.SQL(
                f"SELECT {write_pg.ACTION_COLUMNS} "
                "FROM {}.memory_action_events "
                "ORDER BY ingest_seq").format(
                _sql.Identifier(schema)))
        columns = [d.name for d in cur.description]
        return [write_pg._action_from_row(dict(zip(columns, row)))
                for row in cur.fetchall()]


def do_record_show(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.write import postgres as write_pg

    schema = schema_from(payload)
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "record_show requires record_id")
    connection = raw_connect()
    try:
        record = write_pg.get_record(connection, schema,
                                     record_id.strip())
        if record is None:
            return {"ok": False, "schema": schema,
                    "message": "WRITE_TARGET_NOT_FOUND"}
        actions = write_pg.actions_for_record(
            connection, schema, record.record_id)
        successors = write_pg.successors_of(
            connection, schema, record.record_id)
        outgoing = write_pg.relations_from(
            connection, schema, record.record_id)
    finally:
        connection.close()
    return {"ok": True, "schema": schema,
            "record": record.to_dict(),
            "actions": [a.to_dict() for a in actions],
            "successors": successors, "outgoing": outgoing}


def do_action_show(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.write import postgres as write_pg

    schema = schema_from(payload)
    action_id = payload.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "action_show requires action_id")
    connection = raw_connect()
    try:
        event = write_pg.get_action(connection, schema,
                                    action_id.strip())
        if event is None:
            return {"ok": False, "schema": schema,
                    "message": "WRITE_ACTION_NOT_FOUND"}
        record = None
        if event.new_record_id:
            record = write_pg.get_record(connection, schema,
                                         event.new_record_id)
    finally:
        connection.close()
    return {"ok": True, "schema": schema,
            "action": event.to_dict(),
            "record": record.to_dict() if record else None}


def do_write_history(payload: dict[str, Any]) -> dict[str, Any]:
    """Developer inspection of one explicit-memory lineage: the
    record, what created it, and the chain it belongs to."""
    require_engine()
    from remembering.write import postgres as write_pg

    schema = schema_from(payload)
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise BridgeError("CONFIG_INVALID",
                          "write_history requires record_id")
    connection = raw_connect()
    try:
        record = write_pg.get_record(connection, schema,
                                     record_id.strip())
        if record is None:
            return {"ok": False, "schema": schema,
                    "message": "WRITE_TARGET_NOT_FOUND"}
        lineage = [r for r in write_pg.all_records(
            connection, schema)
            if r.lineage_root == record.lineage_root]
        relations = [r for r in write_pg.all_relations(
            connection, schema)
            if r["target_record_id"] in
            {r.record_id for r in lineage}
            or r["source_record_id"] in
            {r.record_id for r in lineage}]
    finally:
        connection.close()
    chain = sorted(lineage,
                   key=lambda r: (r.recorded_at, r.record_id))
    return {"ok": True, "schema": schema,
            "lineage_root": record.lineage_root,
            "records": [r.to_dict() for r in chain],
            "relations": relations}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: remembering_bridge.py "
            "<doctor|setup|refresh|search|context|capture_session|"
            "route_eval|temporal_import|state|temporal_eval|frame_eval|"
            "trust_import|trust_eval|selection_eval|trace|trace_eval|" "loops|loop_eval|loop_rebuild|loop_create|loop_import|" "remember|write_eval|write_rebuild|record_show|" "action_show|write_history>")
    action = sys.argv[1]
    payload = read_payload()

    if action == "doctor":
        result = doctor(payload)
    elif action == "setup":
        result = do_setup(payload)
    elif action == "refresh":
        result = do_refresh(payload)
    elif action == "search":
        result = do_search(payload)
    elif action == "context":
        result = do_context(payload)
    elif action == "capture_session":
        result = do_capture_session(payload)
    elif action == "route_eval":
        result = do_route_eval(payload)
    elif action == "temporal_import":
        result = do_temporal_import(payload)
    elif action == "state":
        result = do_state(payload)
    elif action == "temporal_eval":
        result = do_temporal_eval(payload)
    elif action == "frame_eval":
        result = do_frame_eval(payload)
    elif action == "trust_import":
        result = do_trust_import(payload)
    elif action == "trust_eval":
        result = do_trust_eval(payload)
    elif action == "selection_eval":
        result = do_selection_eval(payload)
    elif action == "trace":
        result = do_trace(payload)
    elif action == "trace_eval":
        result = do_trace_eval(payload)
    elif action == "loops":
        result = do_loops(payload)
    elif action == "loop_eval":
        result = do_loop_eval(payload)
    elif action == "loop_rebuild":
        result = do_loop_rebuild(payload)
    elif action == "loop_create":
        result = do_loop_create(payload)
    elif action == "loop_import":
        result = do_loop_import(payload)
    elif action == "remember":
        result = do_remember(payload)
    elif action == "write_eval":
        result = do_write_eval(payload)
    elif action == "write_rebuild":
        result = do_write_rebuild(payload)
    elif action == "record_show":
        result = do_record_show(payload)
    elif action == "action_show":
        result = do_action_show(payload)
    elif action == "write_history":
        result = do_write_history(payload)
    else:
        raise BridgeError("CONFIG_INVALID",
                          f"unknown bridge action: {action}")

    emit(result)


if __name__ == "__main__":
    try:
        main()
    except BridgeError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        raise SystemExit(2)
    except Exception as exc:  # Fail loudly and keep stdout parseable.
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise
