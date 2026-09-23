import type { Pool } from "pg";

import { ident } from "../db";
import { EngineError } from "../errors";
import { TEMPORAL_EVENT_SCHEMA, TEMPORAL_STORE_VERSION, type TemporalEnvelope } from "./model";

export interface TemporalEventStore {
  initialise(): Promise<void>;
  append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }>;
  list(): Promise<TemporalEnvelope[]>;
  count(): Promise<number>;
  subjects(): Promise<string[]>;
  versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }>;
}

/** In-memory store: unit tests and engine fallback before PG init. */
export class MemoryTemporalStore implements TemporalEventStore {
  private events = new Map<string, TemporalEnvelope>();
  async initialise(): Promise<void> {}
  async append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }> {
    const prior = this.events.get(envelope.eventId);
    if (prior) {
      const same = JSON.stringify(prior) === JSON.stringify(envelope);
      if (!same) throw new EngineError("TEMPORAL_CONFLICT", `conflicting reuse of event identity: ${envelope.eventId}.`);
      return { duplicate: true };
    }
    this.events.set(envelope.eventId, envelope);
    return { duplicate: false };
  }
  async list(): Promise<TemporalEnvelope[]> {
    return [...this.events.values()];
  }
  async count(): Promise<number> {
    return this.events.size;
  }
  async subjects(): Promise<string[]> {
    return [...new Set([...this.events.values()].map((e) => e.subject))].sort();
  }
  async versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }> {
    return { storeVersion: TEMPORAL_STORE_VERSION, eventSchemaVersion: TEMPORAL_EVENT_SCHEMA };
  }
}

export class PostgresTemporalStore implements TemporalEventStore {
  constructor(private readonly pool: Pool, private readonly schema: string) {}
  async initialise(): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.temporal_events (` +
          `event_id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, ` +
          `value TEXT, event_time TIMESTAMPTZ NOT NULL, known_time TIMESTAMPTZ NOT NULL, ` +
          `effective_from TIMESTAMPTZ NOT NULL, supersedes TEXT, reason TEXT, received_at TIMESTAMPTZ NOT NULL)`,
      );
      await client.query(`CREATE INDEX IF NOT EXISTS temporal_events_subject_idx ON ${s}.temporal_events (subject)`);
      await client.query(
        `INSERT INTO ${s}.meta (key, value) VALUES ` +
          `('temporal.store_version', $1), ('temporal.event_schema', $2) ` +
          `ON CONFLICT (key) DO NOTHING`,
        [TEMPORAL_STORE_VERSION, TEMPORAL_EVENT_SCHEMA],
      );
    } finally {
      client.release();
    }
  }
  async append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const existing = await client.query(`SELECT event_id FROM ${s}.temporal_events WHERE event_id = $1`, [envelope.eventId]);
      if ((existing.rowCount ?? 0) > 0) return { duplicate: true };
      await client.query(
        `INSERT INTO ${s}.temporal_events (event_id, subject, kind, value, event_time, known_time, effective_from, supersedes, reason, received_at) ` +
          `VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
        [
          envelope.eventId, envelope.subject, envelope.kind, envelope.value, envelope.eventTime,
          envelope.knownTime ?? envelope.receivedAt, envelope.effectiveFrom ?? envelope.eventTime,
          envelope.supersedes ?? null, envelope.reason ?? null, envelope.receivedAt,
        ],
      );
      return { duplicate: false };
    } finally {
      client.release();
    }
  }
  async list(): Promise<TemporalEnvelope[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(
        `SELECT event_id, subject, kind, value, event_time, known_time, effective_from, supersedes, reason, received_at ` +
          `FROM ${s}.temporal_events ORDER BY known_time, event_id`,
      );
      return res.rows.map((r) => ({
        eventId: r.event_id, subject: r.subject, kind: r.kind, value: r.value,
        eventTime: new Date(r.event_time).toISOString(),
        knownTime: new Date(r.known_time).toISOString(),
        effectiveFrom: new Date(r.effective_from).toISOString(),
        supersedes: r.supersedes ?? undefined, reason: r.reason ?? undefined,
        receivedAt: new Date(r.received_at).toISOString(),
      }));
    } finally {
      client.release();
    }
  }
  async count(): Promise<number> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT count(*) AS count FROM ${s}.temporal_events`);
      return Number((res.rows[0] as Record<string, unknown>)?.["count"] ?? 0);
    } finally {
      client.release();
    }
  }
  async subjects(): Promise<string[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT DISTINCT subject FROM ${s}.temporal_events ORDER BY 1`);
      return res.rows.map((r) => String(r.subject));
    } finally {
      client.release();
    }
  }
  async versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT key, value FROM ${s}.meta WHERE key LIKE 'temporal.%'`);
      const map = new Map(res.rows.map((r) => [r.key as string, r.value as string]));
      return {
        storeVersion: map.get("temporal.store_version") ?? null,
        eventSchemaVersion: map.get("temporal.event_schema") ?? null,
      };
    } catch {
      return { storeVersion: null, eventSchemaVersion: null };
    } finally {
      client.release();
    }
  }
}
