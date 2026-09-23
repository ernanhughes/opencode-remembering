# Bundled remembering engine (opencode-remembering product code).
"""Append-only standing-event persistence and resolution.

Revocation is standing, not content: a revoked source stays
searchable history but loses permission to guide behaviour.
Corrections to content live in temporal memory; standing changes
live here. Same envelope discipline: exact replays are idempotent,
conflicting identity reuse fails visibly.
"""

from __future__ import annotations

from .model import (
    STANDING_EVENT_SCHEMA,
    STANDING_EVENT_TYPES,
    StandingEvent,
)

STORE_VERSION = "standing-store-v0.1"


def validate_standing_event(raw: dict, known_ids: set[str]) -> StandingEvent:
    if not isinstance(raw, dict):
        raise ValueError("TRUST_EVENT_INVALID:event must be an object")
    try:
        event = StandingEvent.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"TRUST_EVENT_INVALID:malformed envelope: {exc}") from exc
    from ..temporal.ordering import parse_ts

    errors: list[str] = []
    if not event.event_id:
        errors.append("EMPTY_EVENT_ID")
    if event.event_id in known_ids:
        errors.append(f"DUPLICATE_EVENT_ID:{event.event_id}")
    if event.event_type not in STANDING_EVENT_TYPES:
        errors.append(f"UNKNOWN_EVENT_TYPE:{event.event_type}")
    if not event.subject_id:
        errors.append("EMPTY_SUBJECT_ID")
    if parse_ts(event.event_time) is None:
        errors.append(f"BAD_EVENT_TIME:{event.event_time}")
    if parse_ts(event.recorded_at) is None:
        errors.append(f"BAD_RECORDED_AT:{event.recorded_at}")
    if event.effective_from and parse_ts(event.effective_from) is None:
        errors.append(f"BAD_EFFECTIVE_FROM:{event.effective_from}")
    if errors:
        raise ValueError(f"TRUST_EVENT_INVALID:{';'.join(errors)}")
    return event


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
                CREATE TABLE IF NOT EXISTS {}.standing_events (
                    event_id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL DEFAULT 'source',
                    subject_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time TIMESTAMPTZ NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    effective_from TIMESTAMPTZ,
                    scope TEXT NOT NULL DEFAULT '',
                    evidence_refs TEXT[] NOT NULL DEFAULT '{{}}',
                    received_at TIMESTAMPTZ NOT NULL,
                    ingest_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
                )
                """
            ).format(s)
        )
        for name, column in (
            ("standing_events_subject_idx", "subject_id"),
            ("standing_events_recorded_idx", "recorded_at"),
        ):
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON "
                        "{}.standing_events ({})").format(
                    sql.Identifier(f"{schema}_{name}"),
                    s,
                    sql.Identifier(column),
                )
            )
        for key, value in (
            ("standing.store_version", STORE_VERSION),
        ):
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(s),
                (key, value),
            )
    return {"store_version": STORE_VERSION}


def _iso(value):
    from datetime import timezone

    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(conn, schema: str, event: StandingEvent,
                 received_at: str) -> dict:
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, subject_type, subject_id, event_type, "
                "event_time, recorded_at, effective_from, scope, "
                "evidence_refs FROM {}.standing_events "
                "WHERE event_id = %s"
            ).format(s),
            (event.event_id,),
        )
        row = cur.fetchone()
        if row is not None:
            columns = [d.name for d in cur.description]
            record = dict(zip(columns, row))
            existing = StandingEvent(
                event_id=record["event_id"],
                subject_type=record["subject_type"],
                subject_id=record["subject_id"],
                event_type=record["event_type"],
                event_time=_iso(record["event_time"]),
                recorded_at=_iso(record["recorded_at"]),
                effective_from=_iso(record["effective_from"]),
                scope=record["scope"] or "",
                evidence_refs=tuple(record["evidence_refs"] or ()),
            )
            if existing.to_dict() == event.to_dict():
                return {"appended": False, "duplicate": True,
                        "event_id": event.event_id}
            raise ValueError(
                f"TRUST_DUPLICATE_CONFLICT:{event.event_id}: conflicting "
                "reuse of a standing-event identity")
        known = known_ids(conn, schema)
        validated = validate_standing_event(event.to_dict(), known)
        from ..temporal.ordering import parse_ts

        if parse_ts(received_at) is None:
            raise ValueError(
                f"TRUST_EVENT_INVALID:received_at {received_at!r} "
                "is not ISO-8601")
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.standing_events "
                "(event_id, subject_type, subject_id, event_type, "
                "event_time, recorded_at, effective_from, scope, "
                "evidence_refs, received_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(s),
            (
                validated.event_id, validated.subject_type,
                validated.subject_id, validated.event_type,
                validated.event_time, validated.recorded_at,
                validated.effective_from or None, validated.scope,
                list(validated.evidence_refs), received_at,
            ),
        )
    return {"appended": True, "duplicate": False,
            "event_id": event.event_id}


def known_ids(conn, schema: str) -> set[str]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT event_id FROM {}.standing_events").format(
                sql.Identifier(schema)))
        return {row[0] for row in cur.fetchall()}


def load_events(conn, schema: str) -> list[StandingEvent]:
    from psycopg import sql

    out: list[StandingEvent] = []
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT event_id, subject_type, subject_id, event_type, "
                "event_time, recorded_at, effective_from, scope, "
                "evidence_refs FROM {}.standing_events "
                "ORDER BY ingest_seq"
            ).format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        for row in cur.fetchall():
            record = dict(zip(columns, row))
            out.append(StandingEvent(
                event_id=record["event_id"],
                subject_type=record["subject_type"],
                subject_id=record["subject_id"],
                event_type=record["event_type"],
                event_time=_iso(record["event_time"]),
                recorded_at=_iso(record["recorded_at"]),
                effective_from=_iso(record["effective_from"]),
                scope=record["scope"] or "",
                evidence_refs=tuple(record["evidence_refs"] or ()),
            ))
    return out


def event_count(conn, schema: str) -> int:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.standing_events").format(
                sql.Identifier(schema)))
        return int(cur.fetchone()[0])
