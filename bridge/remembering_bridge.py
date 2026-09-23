#!/usr/bin/env python3
"""Process boundary between OpenCode and the bundled remembering engine.

This bridge contains no memory policy of its own. It imports the
plugin's bundled engine (engine/remembering: PostgreSQL store, ingestion, embedding, retrieval, routing
and context modules and exposes a small JSON-over-stdin command
surface:

    doctor | setup | refresh | search | context | capture_session
    | route_eval | temporal_import | state | temporal_eval
    | frame_eval | trust_import | trust_eval

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
            trust_check = validate_trust_config(project_dir)
            if trust_check["error"]:
                raise BridgeError("TRUST_POLICY_INVALID",
                                  trust_check["error"])
            state["baseline"] = baseline
            state["temporal_versions"] = temporal_versions
            return (f"schema {schema} initialised "
                    f"(embedding dim {state['dimension']}; "
                    f"{temporal_versions['store_version']}; "
                    f"{standing_versions['store_version']}; "
                    f"trust policy {trust_check['detail']})")

        _timed(steps, "initialise", s_initialise)

        def s_refresh():
            baseline = state["baseline"]
            refresh = baseline.refresh(Path(os.path.realpath(project_dir)))
            temporal = import_temporal_events(
                baseline.store.conn, schema, project_dir)
            standing = import_standing_events(
                baseline.store.conn, schema, project_dir)
            refresh_dict = refresh_report_to_dict(refresh)
            refresh_dict["schema"] = schema
            refresh_dict["temporal"] = temporal
            refresh_dict["trust"] = standing
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

            standing_pg.initialise(baseline.store.conn, schema)
            refresh = baseline.refresh(Path(os.path.realpath(project_dir)))
            temporal = import_temporal_events(
                baseline.store.conn, schema, project_dir)
            standing = import_standing_events(
                baseline.store.conn, schema, project_dir)
            out = refresh_report_to_dict(refresh)
            out["schema"] = schema
            out["temporal"] = temporal
            out["trust"] = standing
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
                 "role": ""}
        for field in ("claim_key", "role"):
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


def apply_trust_gate(schema: str, project_dir: Path,
                     ranked: list, by_annotation: dict,
                     frame_control: str, route_value: str,
                     trust_spec: dict, enforce: bool) -> dict[str, Any]:
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
    connection = raw_connect()
    try:
        standing_pg.initialise(connection, schema)
        events = standing_pg.load_events(connection, schema)
    finally:
        connection.close()
    standing = resolve_standing(events)

    from remembering.trust.policy import match_rule

    candidates: list[TrustCandidate] = []
    for chunk in ranked:
        annotation = by_annotation.get(chunk.chunk_id)
        rule, rule_id = match_rule(policy, chunk.source_id)
        source_class = (rule.source_class if rule is not None
                        else policy.default_source_class)
        role = None
        claim = claims.get(chunk.source_id, {})
        if claim.get("role"):
            role = claim["role"]
        elif rule is not None:
            role = rule.role
        else:
            role = "ordinary"
        restricted = list((rule.restricted_to if rule is not None else ()))
        candidates.append(TrustCandidate(
            unit_id=chunk.chunk_id, source_id=chunk.source_id,
            project_id=schema, text=chunk.text,
            source_class=source_class, role=role or "ordinary",
            temporal_status=(annotation.status if annotation
                             else "not_modelled"),
            frame_control=frame_control,
            claim_key=claim.get("claim_key", ""),
            derived_from=tuple(claim.get("derived_from", [])),
            refuted_by=tuple(claim.get("refuted_by", [])),
            restricted_to=tuple(restricted)))
    derived_map = {c.source_id: list(c.derived_from) for c in candidates}
    known = ({c.source_id for c in candidates}
             | set(standing_pg_known_subjects(events)))
    ctx = TrustContext(
        policy=policy, revoked=standing["revoked"],
        restricted=standing["restricted"], derived_from=derived_map,
        known_sources=known, project_id=schema,
        caller_scope=trust_spec["caller_scope"],
        level=trust_spec["level"])
    records = judge_all(candidates, ctx)
    latency_ms = round((_time.perf_counter() - started) * 1000, 2)
    admitted = [r.unit_id for r in records if r.verdict.value == "admit"]
    denied = [r.unit_id for r in records if r.verdict.value == "deny"]
    quarantined = [r.unit_id for r in records
                   if r.verdict.value == "quarantine"]
    classes = {c.unit_id: c.source_class for c in candidates}
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
            "records": [r.to_dict() for r in records],
            "admitted_ids": admitted,
            "denied_ids": denied,
            "quarantined_ids": quarantined,
            "latency_ms": latency_ms,
        },
        "records_by_id": {r.unit_id: r for r in records},
        "classes_by_id": classes,
        "admitted_ids": set(admitted),
    }


def standing_pg_known_subjects(events) -> set[str]:
    return {e.subject_id for e in events}


def do_context(payload: dict[str, Any]) -> dict[str, Any]:
    require_engine()
    from remembering.baseline.config import ContextConfig
    from remembering.baseline.context import assemble
    from remembering.baseline.routing import guidance_for, route_for_request

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
    try:
        decision = route_for_request(
            query if isinstance(query, str) else "", requested)
    except ValueError as exc:
        raise BridgeError("CONFIG_INVALID", str(exc))
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
        enforce=enforce_trust)
    trust_block = trust_out["block"]
    trust_records = trust_out["records_by_id"]
    trust_classes = trust_out["classes_by_id"]
    if enforce_trust:
        admitted_trust = trust_out["admitted_ids"]
        ranked_selected = [c for c in ranked_selected
                           if c.chunk_id in admitted_trust]
    trace_trust_latency = trust_block["latency_ms"]
    bundle = assemble(ranked_selected,
                      ContextConfig(max_chars=max_chars,
                                    max_passages=max_results))
    # Route guidance is bundle metadata, but it still counts toward the
    # byte budget: the bundle as a whole stays bounded.
    guidance_prefix = guidance + "\n\n---\n\n"
    evidence_budget = max_chars - len(guidance_prefix)
    parts: list[str] = []
    admitted: list[dict[str, Any]] = []
    by_id = {c["chunk_id"]: c for c in pool_items}
    framed_by_id = {c.chunk_id: c for c in kept}
    chars = 0
    if evidence_budget > 0:
        for chunk in bundle.admitted:
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
            section = f" | {item['section']}" if item["section"] else ""
            header = (f"[source: {item['source_id']}{section} | "
                      f"chunk: {item['chunk_id']} | rank: {item['rank']} | "
                      f"score: {item['score']:.4f} | "
                      f"temporal: {temporal_status}{frame_tag}"
                      f"{standing_tag}]\n")
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
            if len(text) > room:
                marker = "\n[…truncated to fit the context budget…]"
                text = text[:max(0, room - len(marker))] + marker
            piece = separator + header + text
            parts.append(piece)
            chars += len(piece)
            admitted_item = {**item, "text": text}
            if annotation is not None:
                admitted_item["temporal"] = annotation.to_dict()
            if trust_record is not None:
                admitted_item["trust"] = trust_record.to_dict()
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
        "selected_ids": [i["chunk_id"] for i in admitted],
        "dropped_duplicates": bundle.dropped_duplicates,
        "dropped_over_budget": bundle.dropped_over_budget,
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
             "trust": trust_block}

    trace_material = json.dumps(
        {"schema": result["schema"], "query": payload.get("query", ""),
         "mode": result["trace"]["mode"],
         "route": route_block["route"],
         "route_source": route_block["route_source"],
         "temporal_mode": standpoint.mode,
         "valid_at": standpoint.valid_at,
         "known_at": standpoint.known_at,
         "frame_control": frame_trace.get("control"),
         "project_frame_version": frame_trace.get("project_frame_version"),
         "trust_policy_version": trust_block.get("policy_version"),
         "trust_policy_digest": trust_block.get("policy_digest"),
         "embedding": result["trace"]["embedding"]["version"],
         "chunks": [item["chunk_id"] for item in admitted]},
        sort_keys=True,
    )
    trace_id = "hybrid:" + hashlib.sha256(
        trace_material.encode("utf-8")).hexdigest()[:16]
    evidence = "\n\n---\n\n".join(parts)
    content = guidance + "\n\n---\n\n" + evidence if evidence else guidance
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


def do_frame_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Stage 4 frame contract (no services)."""
    _ = payload
    require_engine()
    from remembering.frame.evaluation import evaluate_frame

    report = evaluate_frame()
    return {"ok": bool(report["passed"]), **report}


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

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: remembering_bridge.py "
            "<doctor|setup|refresh|search|context|capture_session|"
            "route_eval|temporal_import|state|temporal_eval|frame_eval|"
            "trust_import|trust_eval>")
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
