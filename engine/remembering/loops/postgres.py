# Bundled remembering engine (opencode-remembering product code).
"""PostgreSQL loop storage: append-only canonical events plus a
rebuildable derived projection. Same envelope discipline as temporal
and standing stores: exact replays are idempotent, conflicting
identity reuse fails as LOOP_EVENT_CONFLICT."""

from __future__ import annotations

from .model import LOOP_EVENT_SCHEMA

STORE_VERSION = "loop-store-v0.1"
PROJECTION_VERSION = "loop-projection-v0.1"


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
                CREATE TABLE IF NOT EXISTS {}.open_loop_events (
                    event_id TEXT PRIMARY KEY,
                    loop_id TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    event_time TIMESTAMPTZ NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    evidence_refs TEXT[] NOT NULL DEFAULT '{{}}',
                    payload JSONB NOT NULL DEFAULT '{{}}',
                    schema_version TEXT NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL,
                    ingest_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
                )
                """
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.open_loops (
                    loop_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL,
                    transition_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected JSONB NOT NULL DEFAULT '{{}}',
                    closure JSONB NOT NULL DEFAULT '{{}}',
                    created_at TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ,
                    last_event_id TEXT NOT NULL,
                    projection_version TEXT NOT NULL
                )
                """
            ).format(s)
        )
        for name, table, column in (
            ("loop_events_loop_idx", "open_loop_events", "loop_id"),
            ("loop_events_recorded_idx", "open_loop_events",
             "recorded_at"),
            ("loops_state_idx", "open_loops", "state"),
            ("loops_subject_idx", "open_loops", "subject"),
        ):
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                    sql.Identifier(f"{schema}_{name}"),
                    s,
                    sql.Identifier(table),
                    sql.Identifier(column),
                )
            )
        for key, value in (
            ("loops.store_version", STORE_VERSION),
            ("loops.event_schema_version", LOOP_EVENT_SCHEMA),
            ("loops.projection_version", PROJECTION_VERSION),
        ):
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(s),
                (key, value),
            )
    return {"store_version": STORE_VERSION,
            "event_schema_version": LOOP_EVENT_SCHEMA,
            "projection_version": PROJECTION_VERSION}


def _iso(value):
    from datetime import timezone

    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(conn, schema: str, event, received_at: str) -> dict:
    from psycopg import sql

    from .events import validate_event

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, loop_id, project_id, event_type, "
                "event_time, recorded_at, evidence_refs, payload, "
                "schema_version FROM {}.open_loop_events "
                "WHERE event_id = %s"
            ).format(s),
            (event.event_id,),
        )
        row = cur.fetchone()
        if row is not None:
            from ..temporal.ordering import parse_ts as _parse_ts

            columns = [d.name for d in cur.description]
            record = dict(zip(columns, row))
            stored_payload = record["payload"] or {}
            if not isinstance(stored_payload, dict):
                stored_payload = dict(stored_payload)
            new_payload = dict(event.payload)
            same = (
                record["event_id"] == event.event_id
                and record["loop_id"] == event.loop_id
                and (record["project_id"] or "") == event.project_id
                and record["event_type"] == event.event_type
                and _parse_ts(_iso(record["event_time"])) == _parse_ts(
                    event.event_time)
                and _parse_ts(_iso(record["recorded_at"])) == _parse_ts(
                    event.recorded_at)
                and list(record["evidence_refs"] or []) == list(
                    event.evidence_refs)
                and stored_payload == new_payload
                and (record["schema_version"] or "") ==
                event.schema_version
            )
            if same:
                return {"appended": False, "duplicate": True,
                        "event_id": event.event_id}
            raise ValueError(
                f"LOOP_EVENT_CONFLICT:{event.event_id}: conflicting "
                "reuse of a loop-event identity")
        known = known_ids(conn, schema)
        problems = validate_event(event, known)
        if problems:
            raise ValueError(
                f"LOOP_EVENT_INVALID:{';'.join(problems)}")
        from ..temporal.ordering import parse_ts

        if parse_ts(received_at) is None:
            raise ValueError(
                f"LOOP_EVENT_INVALID:received_at {received_at!r} "
                "is not ISO-8601")
        import json

        cur.execute(
            sql.SQL(
                "INSERT INTO {}.open_loop_events "
                "(event_id, loop_id, project_id, event_type, event_time, "
                "recorded_at, evidence_refs, payload, schema_version, "
                "received_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(s),
            (
                event.event_id, event.loop_id, event.project_id,
                event.event_type, event.event_time, event.recorded_at,
                list(event.evidence_refs),
                json.dumps(dict(event.payload)), event.schema_version,
                received_at,
            ),
        )
    return {"appended": True, "duplicate": False,
            "event_id": event.event_id}


def known_ids(conn, schema: str) -> set[str]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT event_id FROM {}.open_loop_events").format(
                sql.Identifier(schema)))
        return {row[0] for row in cur.fetchall()}


def load_events(conn, schema: str) -> list:
    from psycopg import sql

    from .model import OpenLoopEvent

    out: list[OpenLoopEvent] = []
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, loop_id, project_id, event_type, "
                "event_time, recorded_at, evidence_refs, payload, "
                "schema_version FROM {}.open_loop_events "
                "ORDER BY ingest_seq"
            ).format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(columns, row))
            raw_payload = record["payload"] or {}
            pairs = (raw_payload.items() if isinstance(raw_payload, dict)
                     else raw_payload)
            out.append(OpenLoopEvent(
                event_id=record["event_id"], loop_id=record["loop_id"],
                project_id=record["project_id"] or "",
                event_type=record["event_type"],
                event_time=_iso(record["event_time"]),
                recorded_at=_iso(record["recorded_at"]),
                evidence_refs=tuple(record["evidence_refs"] or ()),
                payload=tuple((str(k), str(v)) for k, v in pairs),
                schema_version=record["schema_version"],
            ))
    return out


def event_count(conn, schema: str) -> int:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.open_loop_events").format(
                sql.Identifier(schema)))
        return int(cur.fetchone()[0])


def write_projection(conn, schema: str, views: list) -> int:
    """Replace the derived projection (rebuildable, never canonical)."""
    from psycopg import sql

    import json

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DELETE FROM {}.open_loops").format(s))
        for view in views:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.open_loops "
                    "(loop_id, project_id, subject, transition_kind, "
                    "state, reason, expected, closure, created_at, "
                    "resolved_at, last_event_id, projection_version) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s)"
                ).format(s),
                (
                    view.loop_id, view.project_id, view.subject,
                    view.transition_kind, view.state.value, view.reason,
                    json.dumps(view.expected),
                    json.dumps(view.closure),
                    view.created_at or None, view.resolved_at or None,
                    (view.history[-1]["event_id"] if view.history
                     else ""),
                    PROJECTION_VERSION,
                ),
            )
    return len(views)


def versions(conn, schema: str) -> dict:
    from psycopg import sql

    out = {"store_version": None, "event_schema_version": None,
           "projection_version": None}
    mapping = {"loops.store_version": "store_version",
               "loops.event_schema_version": "event_schema_version",
               "loops.projection_version": "projection_version"}
    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL("SELECT key, value FROM {}.meta "
                        "WHERE key LIKE 'loops.%%'").format(
                    sql.Identifier(schema)))
            for key, value in cur.fetchall():
                if key in mapping:
                    out[mapping[key]] = value
        except Exception:
            pass
    return out
