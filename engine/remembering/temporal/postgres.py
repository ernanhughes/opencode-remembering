# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""PostgreSQL persistence for the temporal event model.

Generic storage, no client concepts: envelopes go in, an ``EventLog``
comes back out, and every semantic check (validation, gaps, unknown
refs, replay) runs through the existing in-memory implementation.
Derived projections stay rebuildable: same events + same reducer =
same state, verified by digest.

Table layout per project schema::

    <schema>.temporal_events

Scalar identity/time columns are typed (TIMESTAMPTZ / INT) for
standpoint queries; tuple/list relations ride in TEXT[] / JSONB.
``ingest_seq`` (identity) preserves arrival order so a reload replays
exactly the original log, including out-of-order arrivals and gaps.
"""

from __future__ import annotations

from .log import EventLog
from .model import SCHEMA_VERSION as EVENT_SCHEMA_VERSION
from .model import REDUCER_VERSION, EventEnvelope

STORE_VERSION = "temporal-store-v0.1"


def initialise(conn, schema: str) -> dict:
    """Create the temporal table, indexes, and version metadata.

    Idempotent; never migrates or rewrites existing rows.
    """
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
                CREATE TABLE IF NOT EXISTS {}.temporal_events (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_seq INT NOT NULL,
                    event_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    state_key TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL DEFAULT '',
                    event_time TIMESTAMPTZ NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    effective_from TIMESTAMPTZ,
                    effective_to TIMESTAMPTZ,
                    causal_parents TEXT[] NOT NULL DEFAULT '{{}}',
                    supersedes TEXT[] NOT NULL DEFAULT '{{}}',
                    corrects TEXT[] NOT NULL DEFAULT '{{}}',
                    evidence_refs TEXT[] NOT NULL DEFAULT '{{}}',
                    payload JSONB NOT NULL DEFAULT '[]',
                    schema_version TEXT NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL,
                    ingest_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
                )
                """
            ).format(s)
        )
        for name, column in (
            ("temporal_events_subject_idx", "subject"),
            ("temporal_events_state_key_idx", "state_key"),
            ("temporal_events_event_time_idx", "event_time"),
            ("temporal_events_recorded_at_idx", "recorded_at"),
            ("temporal_events_source_idx", "source_id"),
        ):
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.temporal_events "
                        "({})").format(
                    sql.Identifier(f"{schema}_{name}"),
                    s,
                    sql.Identifier(column),
                )
            )
        for key, value in (
            ("temporal.store_version", STORE_VERSION),
            ("temporal.event_schema_version", EVENT_SCHEMA_VERSION),
            ("temporal.reducer_version", REDUCER_VERSION),
        ):
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(s),
                (key, value),
            )
    return {
        "store_version": STORE_VERSION,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "reducer_version": REDUCER_VERSION,
    }


def _row_to_envelope(row: dict) -> EventEnvelope:
    def iso(value):
        # Canonical UTC lexical form ("...Z"): timestamptz values must
        # round-trip to byte-identical envelopes for digest equivalence,
        # regardless of the server's TimeZone setting.
        from datetime import timezone

        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z")

    return EventEnvelope(
        event_id=row["event_id"],
        source_id=row["source_id"],
        source_seq=int(row["source_seq"]),
        event_type=row["event_type"],
        subject=row["subject"],
        state_key=row.get("state_key") or "",
        value=row.get("value") or "",
        event_time=iso(row["event_time"]),
        recorded_at=iso(row["recorded_at"]),
        effective_from=iso(row.get("effective_from")),
        effective_to=iso(row.get("effective_to")),
        causal_parents=tuple(row.get("causal_parents") or ()),
        supersedes=tuple(row.get("supersedes") or ()),
        corrects=tuple(row.get("corrects") or ()),
        evidence_refs=tuple(row.get("evidence_refs") or ()),
        payload=tuple(tuple(p) for p in (row.get("payload") or [])),
        schema_version=row.get("schema_version") or EVENT_SCHEMA_VERSION,
    )


def append_event(conn, schema: str, event: EventEnvelope,
                 received_at: str) -> dict:
    """Append one envelope. Exact replays are idempotent no-ops;
    conflicting reuse of an event identity fails visibly; invalid
    envelopes fail with the model's validation codes. Nothing is
    ever silently mutated."""
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, source_id, source_seq, event_type, "
                "subject, state_key, value, event_time, recorded_at, "
                "effective_from, effective_to, causal_parents, supersedes, "
                "corrects, evidence_refs, payload, schema_version "
                "FROM {}.temporal_events WHERE event_id = %s"
            ).format(s),
            (event.event_id,),
        )
        row = cur.fetchone()
        if row is not None:
            columns = [d.name for d in cur.description]
            existing = _row_to_envelope(dict(zip(columns, row)))
            if existing.to_dict() == event.to_dict():
                return {"appended": False, "duplicate": True,
                        "event_id": event.event_id}
            raise ValueError(
                f"TEMPORAL_DUPLICATE_CONFLICT:{event.event_id}: "
                "event identity already stored with different content; "
                "history is append-only, use a new event_id")
        # Validate against the stored log before writing.
        log = load_log(conn, schema)
        try:
            log.append(event, received_at)
        except ValueError as exc:
            raise ValueError(f"TEMPORAL_EVENT_INVALID:{exc}") from exc
        from .ordering import parse_ts as _parse_ts

        if _parse_ts(received_at) is None:
            raise ValueError(
                "TEMPORAL_EVENT_INVALID:received_at "
                f"{received_at!r} is not ISO-8601")
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.temporal_events "
                "(event_id, source_id, source_seq, event_type, subject, "
                "state_key, value, event_time, recorded_at, "
                "effective_from, effective_to, causal_parents, supersedes, "
                "corrects, evidence_refs, payload, schema_version, "
                "received_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s)"
            ).format(s),
            (
                event.event_id, event.source_id, event.source_seq,
                event.event_type, event.subject, event.state_key,
                event.value, event.event_time, event.recorded_at,
                event.effective_from, event.effective_to,
                list(event.causal_parents), list(event.supersedes),
                list(event.corrects), list(event.evidence_refs),
                [list(p) for p in event.payload],
                event.schema_version, received_at,
            ),
        )
    return {"appended": True, "duplicate": False,
            "event_id": event.event_id}


def load_log(conn, schema: str) -> EventLog:
    """Reload the stored log in arrival order. Out-of-order arrivals,
    gaps, and deferred references reproduce exactly."""
    from psycopg import sql

    log = EventLog()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, source_id, source_seq, event_type, "
                "subject, state_key, value, event_time, recorded_at, "
                "effective_from, effective_to, causal_parents, supersedes, "
                "corrects, evidence_refs, payload, schema_version, "
                "received_at FROM {}.temporal_events "
                "ORDER BY ingest_seq"
            ).format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(columns, row))
            received = record["received_at"]
            envelope = _row_to_envelope(record)
            log.append(envelope,
                       received.isoformat() if received is not None else "")
    return log


def event_count(conn, schema: str) -> int:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.temporal_events").format(
                sql.Identifier(schema)))
        return int(cur.fetchone()[0])


def subjects(conn, schema: str) -> list[str]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT DISTINCT subject FROM {}.temporal_events "
                "ORDER BY 1").format(sql.Identifier(schema)))
        return [row[0] for row in cur.fetchall()]


def versions(conn, schema: str) -> dict:
    from psycopg import sql

    out = {"store_version": None, "event_schema_version": None,
           "reducer_version": None}
    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL("SELECT key, value FROM {}.meta "
                        "WHERE key LIKE 'temporal.%%'").format(
                    sql.Identifier(schema)))
            for key, value in cur.fetchall():
                short = key.split(".", 1)[1]
                if short in out:
                    out[short] = value
        except Exception:
            pass
    return out
