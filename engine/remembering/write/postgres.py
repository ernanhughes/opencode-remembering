# Bundled remembering engine (opencode-remembering product code).
"""Append-only explicit-memory persistence plus rebuildable projections.

Canonical facts (never updated, never deleted by application code):

- explicit_memory_records: one immutable row per accepted write.
- memory_action_events: one immutable row per accepted action, carrying
  the write-policy identity (version, digest, matched grant) so
  historical authorization stays auditable after policy changes.

Derived facts (rebuildable from the canonical tables):

- explicit_memory_relations: lifecycle edges (corrects, supersedes,
  retracts) for current-state lookup and search annotation.
- retrieval rows (sources/chunks/vectors) under memory://explicit/.
- temporal overlay events (subjects explicit-memory:<lineage root>).

The API surface offers insert/get/list/count only. There is no update
or delete path for canonical records or events.
"""

from __future__ import annotations

from . import (EXPLICIT_RECORD_SCHEMA, MEMORY_ACTION_SCHEMA,
               RELATION_VERSION, WRITE_STORE_VERSION)
from .model import ExplicitMemoryRecord, MemoryActionEvent


def initialise(conn, schema: str) -> dict:
    from psycopg import sql

    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.explicit_memory_records (
                    record_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    standing_ceiling TEXT NOT NULL,
                    actor_kind TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    caller_scope TEXT NOT NULL DEFAULT '',
                    surface TEXT NOT NULL DEFAULT '',
                    event_time TIMESTAMPTZ,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    effective_from TIMESTAMPTZ,
                    evidence_refs JSONB NOT NULL DEFAULT '[]',
                    lineage_root TEXT NOT NULL DEFAULT '',
                    created_by_action TEXT NOT NULL DEFAULT '',
                    schema_version TEXT NOT NULL
                )
                """
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.memory_action_events (
                    action_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    new_record_id TEXT,
                    target_record_id TEXT,
                    relation TEXT,
                    reason TEXT,
                    actor_kind TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    caller_scope TEXT NOT NULL DEFAULT '',
                    surface TEXT NOT NULL DEFAULT '',
                    event_time TIMESTAMPTZ,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    effective_from TIMESTAMPTZ,
                    evidence_refs JSONB NOT NULL DEFAULT '[]',
                    write_policy_version TEXT NOT NULL DEFAULT '',
                    write_policy_digest TEXT NOT NULL DEFAULT '',
                    matched_grant_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT UNIQUE,
                    request_hash TEXT NOT NULL DEFAULT '',
                    host_session_id TEXT NOT NULL DEFAULT '',
                    host_tool_call_id TEXT NOT NULL DEFAULT '',
                    received_at TIMESTAMPTZ NOT NULL,
                    ingest_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                    schema_version TEXT NOT NULL
                )
                """
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.explicit_memory_relations (
                    source_record_id TEXT NOT NULL,
                    target_record_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    effective_from TIMESTAMPTZ,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (source_record_id, target_record_id)
                )
                """
            ).format(s)
        )
        for name, column in (
            ("explicit_records_project_idx", "project_id"),
            ("memory_actions_project_idx", "project_id"),
            ("memory_actions_idem_idx", "idempotency_key"),
            ("memory_relations_target_idx", "target_record_id"),
        ):
            table = ("memory_action_events"
                     if name.startswith("memory_actions")
                     else ("explicit_memory_relations"
                           if name.startswith("memory_relations")
                           else "explicit_memory_records"))
            cur.execute(
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON "
                        "{}.{} ({})").format(
                    sql.Identifier(f"{schema}_{name}"),
                    s, sql.Identifier(table),
                    sql.Identifier(column),
                )
            )
        for key, value in (
            ("write.store_version", WRITE_STORE_VERSION),
            ("write.record_schema_version", EXPLICIT_RECORD_SCHEMA),
            ("write.action_schema_version", MEMORY_ACTION_SCHEMA),
            ("write.relation_version", RELATION_VERSION),
        ):
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING"
                ).format(s),
                (key, value),
            )
    return {"store_version": WRITE_STORE_VERSION,
            "record_schema_version": EXPLICIT_RECORD_SCHEMA,
            "action_schema_version": MEMORY_ACTION_SCHEMA,
            "relation_version": RELATION_VERSION}


def _tables_ready(cur, schema: str) -> bool:
    cur.execute("SELECT to_regclass(%s)",
                (f"{schema}.explicit_memory_records",))
    return cur.fetchone()[0] is not None


def insert_record(conn, schema: str,
                  record: ExplicitMemoryRecord) -> None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.explicit_memory_records "
                "(record_id, project_id, content, content_hash, role, "
                "standing_ceiling, actor_kind, actor_id, caller_scope, "
                "surface, event_time, recorded_at, effective_from, "
                "evidence_refs, lineage_root, created_by_action, "
                "schema_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s)"
            ).format(sql.Identifier(schema)),
            (
                record.record_id, record.project_id, record.content,
                record.content_hash, record.role,
                record.standing_ceiling, record.actor_kind,
                record.actor_id, record.caller_scope, record.surface,
                record.event_time, record.recorded_at,
                record.effective_from,
                list(record.evidence_refs), record.lineage_root,
                record.created_by_action, record.schema_version,
            ),
        )


def insert_action(conn, schema: str, event: MemoryActionEvent,
                  received_at: str) -> None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.memory_action_events "
                "(action_id, project_id, action, new_record_id, "
                "target_record_id, relation, reason, actor_kind, "
                "actor_id, caller_scope, surface, event_time, "
                "recorded_at, effective_from, evidence_refs, "
                "write_policy_version, write_policy_digest, "
                "matched_grant_id, idempotency_key, request_hash, "
                "host_session_id, host_tool_call_id, "
                "received_at, schema_version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(sql.Identifier(schema)),
            (
                event.action_id, event.project_id, event.action,
                event.new_record_id, event.target_record_id,
                event.relation, event.reason, event.actor_kind,
                event.actor_id, event.caller_scope, event.surface,
                event.event_time, event.recorded_at,
                event.effective_from, list(event.evidence_refs),
                event.write_policy_version, event.write_policy_digest,
                event.matched_grant_id, event.idempotency_key,
                event.request_hash, event.host_session_id,
                event.host_tool_call_id, received_at,
                event.schema_version,
            ),
        )


def insert_relation(conn, schema: str, source_record_id: str,
                    target_record_id: str, relation: str,
                    action_id: str, effective_from: str | None,
                    recorded_at: str) -> None:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.explicit_memory_relations "
                "(source_record_id, target_record_id, relation, "
                "action_id, effective_from, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(sql.Identifier(schema)),
            (source_record_id, target_record_id, relation, action_id,
             effective_from, recorded_at),
        )


def _iso(value) -> str | None:
    from datetime import timezone

    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z")


RECORD_COLUMNS = (
    "record_id, project_id, content, content_hash, role, "
    "standing_ceiling, actor_kind, actor_id, caller_scope, surface, "
    "event_time, recorded_at, effective_from, evidence_refs, "
    "lineage_root, created_by_action, schema_version"
)

ACTION_COLUMNS = (
    "action_id, project_id, action, new_record_id, target_record_id, "
    "relation, reason, actor_kind, actor_id, caller_scope, surface, "
    "event_time, recorded_at, effective_from, evidence_refs, "
    "write_policy_version, write_policy_digest, matched_grant_id, "
    "idempotency_key, request_hash, host_session_id, "
    "host_tool_call_id, schema_version"
)


def get_record(conn, schema: str,
               record_id: str) -> ExplicitMemoryRecord | None:
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return None
        cur.execute(
            sql.SQL(
                f"SELECT {RECORD_COLUMNS} "
                "FROM {}.explicit_memory_records WHERE record_id = %s"
            ).format(sql.Identifier(schema)),
            (record_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description]
        data = dict(zip(columns, row))
        return ExplicitMemoryRecord(
            record_id=data["record_id"],
            project_id=data["project_id"],
            content=data["content"],
            content_hash=data["content_hash"] or "",
            role=data["role"],
            standing_ceiling=data["standing_ceiling"],
            actor_kind=data["actor_kind"] or "",
            actor_id=data["actor_id"] or "",
            caller_scope=data["caller_scope"] or "",
            surface=data["surface"] or "",
            event_time=_iso(data["event_time"]),
            recorded_at=_iso(data["recorded_at"]),
            effective_from=_iso(data["effective_from"]),
            evidence_refs=tuple(data["evidence_refs"] or ()),
            lineage_root=data["lineage_root"] or "",
            created_by_action=data["created_by_action"] or "",
            schema_version=data["schema_version"]
            or EXPLICIT_RECORD_SCHEMA,
        )


def get_action(conn, schema: str,
               action_id: str) -> MemoryActionEvent | None:
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return None
        cur.execute(
            sql.SQL(
                f"SELECT {ACTION_COLUMNS} "
                "FROM {}.memory_action_events WHERE action_id = %s"
            ).format(sql.Identifier(schema)),
            (action_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description]
        data = dict(zip(columns, row))
        return _action_from_row(data)


def get_action_by_idempotency(conn, schema: str,
                              idempotency_key: str,
                              ) -> MemoryActionEvent | None:
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return None
        cur.execute(
            sql.SQL(
                f"SELECT {ACTION_COLUMNS} "
                "FROM {}.memory_action_events "
                "WHERE idempotency_key = %s"
            ).format(sql.Identifier(schema)),
            (idempotency_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description]
        return _action_from_row(dict(zip(columns, row)))


def _action_from_row(data: dict) -> MemoryActionEvent:
    return MemoryActionEvent(
        action_id=data["action_id"],
        project_id=data["project_id"],
        action=data["action"],
        new_record_id=data.get("new_record_id"),
        target_record_id=data.get("target_record_id"),
        relation=data.get("relation"),
        reason=data.get("reason"),
        actor_kind=data["actor_kind"] or "",
        actor_id=data["actor_id"] or "",
        caller_scope=data["caller_scope"] or "",
        surface=data["surface"] or "",
        event_time=_iso(data["event_time"]),
        recorded_at=_iso(data["recorded_at"]),
        effective_from=_iso(data["effective_from"]),
        evidence_refs=tuple(data["evidence_refs"] or ()),
        write_policy_version=data["write_policy_version"] or "",
        write_policy_digest=data["write_policy_digest"] or "",
        matched_grant_id=data["matched_grant_id"] or "",
        idempotency_key=data.get("idempotency_key"),
        request_hash=data.get("request_hash") or "",
        host_session_id=data.get("host_session_id") or "",
        host_tool_call_id=data.get("host_tool_call_id") or "",
        schema_version=data.get("schema_version")
        or MEMORY_ACTION_SCHEMA,
    )


def successors_of(conn, schema: str,
                  record_id: str) -> list[dict]:
    """Lifecycle successors of a record (newer records/retractions
    targeting it). Empty means the record is the terminal of its
    lineage."""
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT source_record_id, target_record_id, relation, "
                "action_id, effective_from, recorded_at "
                "FROM {}.explicit_memory_relations "
                "WHERE target_record_id = %s ORDER BY recorded_at"
            ).format(sql.Identifier(schema)),
            (record_id,),
        )
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def relations_from(conn, schema: str,
                   record_id: str) -> list[dict]:
    """Edges a record originates (what it corrects/supersedes)."""
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT source_record_id, target_record_id, relation, "
                "action_id, effective_from, recorded_at "
                "FROM {}.explicit_memory_relations "
                "WHERE source_record_id = %s ORDER BY recorded_at"
            ).format(sql.Identifier(schema)),
            (record_id,),
        )
        columns = [d.name for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def actions_for_record(conn, schema: str,
                       record_id: str) -> list[MemoryActionEvent]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                f"SELECT {ACTION_COLUMNS} "
                "FROM {}.memory_action_events "
                "WHERE new_record_id = %s OR target_record_id = %s "
                "ORDER BY ingest_seq"
            ).format(sql.Identifier(schema)),
            (record_id, record_id),
        )
        columns = [d.name for d in cur.description]
        return [_action_from_row(dict(zip(columns, row)))
                for row in cur.fetchall()]


def all_records(conn, schema: str) -> list[ExplicitMemoryRecord]:
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return []
        cur.execute(
            sql.SQL(
                f"SELECT {RECORD_COLUMNS} "
                "FROM {}.explicit_memory_records "
                "ORDER BY recorded_at, record_id"
            ).format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        out = []
        for row in cur.fetchall():
            data = dict(zip(columns, row))
            out.append(ExplicitMemoryRecord(
                record_id=data["record_id"],
                project_id=data["project_id"],
                content=data["content"],
                content_hash=data["content_hash"] or "",
                role=data["role"],
                standing_ceiling=data["standing_ceiling"],
                actor_kind=data["actor_kind"] or "",
                actor_id=data["actor_id"] or "",
                caller_scope=data["caller_scope"] or "",
                surface=data["surface"] or "",
                event_time=_iso(data["event_time"]),
                recorded_at=_iso(data["recorded_at"]),
                effective_from=_iso(data["effective_from"]),
                evidence_refs=tuple(data["evidence_refs"] or ()),
                lineage_root=data["lineage_root"] or "",
                created_by_action=data["created_by_action"] or "",
                schema_version=data["schema_version"]
                or EXPLICIT_RECORD_SCHEMA,
            ))
        return out


def all_relations(conn, schema: str) -> list[dict]:
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return []
        cur.execute(
            sql.SQL(
                "SELECT source_record_id, target_record_id, relation, "
                "action_id, effective_from, recorded_at "
                "FROM {}.explicit_memory_relations "
                "ORDER BY recorded_at"
            ).format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        out = []
        for row in cur.fetchall():
            item = dict(zip(columns, row))
            item["effective_from"] = _iso(item["effective_from"])
            item["recorded_at"] = _iso(item["recorded_at"])
            out.append(item)
        return out


def counts(conn, schema: str) -> dict:
    from psycopg import sql

    out = {"records": 0, "actions": 0, "relations": 0,
           "remember": 0, "correct": 0, "supersede": 0, "retract": 0,
           "last_action_at": None}
    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return out
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.explicit_memory_records"
                    ).format(sql.Identifier(schema)))
        out["records"] = int(cur.fetchone()[0])
        cur.execute(
            sql.SQL("SELECT action, count(*) FROM {}.memory_action_events "
                    "GROUP BY 1").format(sql.Identifier(schema)))
        total = 0
        for action, count in cur.fetchall():
            total += int(count)
            if action in out:
                out[action] = int(count)
        out["actions"] = total
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.explicit_memory_relations"
                    ).format(sql.Identifier(schema)))
        out["relations"] = int(cur.fetchone()[0])
        cur.execute(
            sql.SQL("SELECT max(recorded_at) FROM {}.memory_action_events"
                    ).format(sql.Identifier(schema)))
        out["last_action_at"] = _iso(cur.fetchone()[0])
    return out


def unresolved_index_records(conn, schema: str) -> list[str]:
    """Explicit records with no retrieval source row. Under atomic
    writes this is always empty; a non-empty list is a visible
    integrity signal, never silently repaired."""
    from psycopg import sql

    with conn.cursor() as cur:
        if not _tables_ready(cur, schema):
            return []
        cur.execute(
            sql.SQL(
                "SELECT r.record_id FROM {}.explicit_memory_records r "
                "LEFT JOIN {}.sources s ON s.source_id = "
                "'memory://explicit/' || r.record_id "
                "WHERE s.source_id IS NULL ORDER BY 1"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
        return [row[0] for row in cur.fetchall()]


def source_attributes(conn, schema: str,
                      source_ids: list[str]) -> dict:
    """Per-source explicit-memory attributes for the read path
    (standing ceiling, authorized role, lineage). Ordinary sources
    are absent from the result; their behavior is unchanged."""
    from psycopg import sql

    wanted = [s for s in source_ids
              if isinstance(s, str)
              and s.startswith("memory://explicit/")]
    if not wanted:
        return {}
    with conn.cursor() as cur:
        try:
            cur.execute(
                sql.SQL(
                    "SELECT record_id, standing_ceiling, role, "
                    "lineage_root, created_by_action, recorded_at "
                    "FROM {}.explicit_memory_records "
                    "WHERE 'memory://explicit/' || record_id = "
                    "ANY(%s)").format(sql.Identifier(schema)),
                (wanted,))
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for row in cur.fetchall():
            out[f"memory://explicit/{row[0]}"] = {
                "record_id": row[0], "standing_ceiling": row[1],
                "role": row[2], "lineage_root": row[3],
                "created_by_action": row[4],
                "recorded_at": _iso(row[5])}
    return out


def rebuild_relations(conn, schema: str) -> dict:
    """Re-derive the relation projection from canonical action
    events. Canonical IDs are untouched."""
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT action_id, action, new_record_id, "
                    "target_record_id, relation, effective_from, "
                    "recorded_at "
                    "FROM {}.memory_action_events "
                    "WHERE relation IS NOT NULL "
                    "ORDER BY ingest_seq").format(sql.Identifier(schema))
        )
        columns = [d.name for d in cur.description]
        events = [dict(zip(columns, row)) for row in cur.fetchall()]
        derived = derive_relations(events)
        cur.execute(
            sql.SQL("DELETE FROM {}.explicit_memory_relations").format(
                sql.Identifier(schema))
        )
        inserted = 0
        for item in derived:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.explicit_memory_relations "
                    "(source_record_id, target_record_id, relation, "
                    "action_id, effective_from, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING"
                ).format(sql.Identifier(schema)),
                (item["source_record_id"],
                 item["target_record_id"], item["relation"],
                 item["action_id"], item["effective_from"],
                 item["recorded_at"]),
            )
            inserted += cur.rowcount
    return {"relations": inserted}


def derive_relations(events: list[dict]) -> list[dict]:
    """Pure derivation: relation projection as a function of
    canonical action events (used by rebuild and evaluation)."""
    out = []
    for event in sorted(events,
                        key=lambda e: (str(e.get("recorded_at") or ""),
                                       str(e.get("action_id") or ""))):
        if event.get("relation") and event.get("target_record_id"):
            out.append({
                "source_record_id": event.get("new_record_id")
                or event.get("action_id"),
                "target_record_id": event.get("target_record_id"),
                "relation": event.get("relation"),
                "action_id": event.get("action_id"),
                "effective_from": event.get("effective_from")
                or event.get("recorded_at"),
                "recorded_at": event.get("recorded_at")})
    return out
