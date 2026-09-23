import type { Pool } from "pg";

import { ident } from "../db";
import { EngineError } from "../errors";
import { LOOP_STORE_VERSION, validateLoopEvent, type LoopEvent } from "./model";

export interface LoopEventStore {
  initialise(): Promise<void>;
  append(event: LoopEvent): Promise<{ duplicate: boolean }>;
  list(): Promise<LoopEvent[]>;
  count(): Promise<number>;
}

export class MemoryLoopStore implements LoopEventStore {
  private events = new Map<string, LoopEvent>();
  async initialise(): Promise<void> {}
  async append(event: LoopEvent): Promise<{ duplicate: boolean }> {
    const prior = this.events.get(event.eventId);
    if (prior) {
      if (JSON.stringify(prior) !== JSON.stringify(event)) {
        throw new EngineError("LOOP_INVALID", `conflicting reuse of loop event identity: ${event.eventId}.`);
      }
      return { duplicate: true };
    }
    this.events.set(event.eventId, event);
    return { duplicate: false };
  }
  async list(): Promise<LoopEvent[]> {
    return [...this.events.values()];
  }
  async count(): Promise<number> {
    return this.events.size;
  }
}

export class PostgresLoopStore implements LoopEventStore {
  constructor(private readonly pool: Pool, private readonly schema: string) {}
  async initialise(): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.open_loop_events (` +
          `event_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, ` +
          `transition_kind TEXT NOT NULL, from_state TEXT, to_state TEXT, expected JSONB NOT NULL DEFAULT '{}', ` +
          `evidence_refs JSONB NOT NULL DEFAULT '[]', closure JSONB NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '', at TIMESTAMPTZ NOT NULL)`,
      );
      await client.query(`CREATE INDEX IF NOT EXISTS open_loop_events_loop_idx ON ${s}.open_loop_events (loop_id)`);
      await client.query(
        `INSERT INTO ${s}.meta (key, value) VALUES ('loops.store_version', $1) ON CONFLICT (key) DO NOTHING`,
        [LOOP_STORE_VERSION],
      );
    } finally {
      client.release();
    }
  }
  async append(event: LoopEvent): Promise<{ duplicate: boolean }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const existing = await client.query(`SELECT event_id FROM ${s}.open_loop_events WHERE event_id = $1`, [event.eventId]);
      if ((existing.rowCount ?? 0) > 0) return { duplicate: true };
      await client.query(
        `INSERT INTO ${s}.open_loop_events (event_id, loop_id, kind, subject, transition_kind, from_state, to_state, expected, evidence_refs, closure, reason, at) ` +
          `VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)`,
        [event.eventId, event.loopId, event.kind, event.subject, event.transitionKind, event.fromState ?? null,
          event.toState ?? null, JSON.stringify(event.expected ?? {}), JSON.stringify(event.evidenceRefs),
          JSON.stringify(event.closure), event.reason, event.at],
      );
      return { duplicate: false };
    } finally {
      client.release();
    }
  }
  async list(): Promise<LoopEvent[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT * FROM ${s}.open_loop_events ORDER BY at, event_id`);
      return res.rows.map((r) => validateLoopEvent({
        event_id: r.event_id, loop_id: r.loop_id, kind: r.kind, subject: r.subject,
        transition_kind: r.transition_kind, from_state: r.from_state ?? undefined, to_state: r.to_state ?? undefined,
        expected: typeof r.expected === "string" ? JSON.parse(r.expected) : r.expected,
        evidence_refs: typeof r.evidence_refs === "string" ? JSON.parse(r.evidence_refs) : r.evidence_refs,
        closure: typeof r.closure === "string" ? JSON.parse(r.closure) : r.closure,
        reason: r.reason, at: new Date(r.at).toISOString(),
      }));
    } finally {
      client.release();
    }
  }
  async count(): Promise<number> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT count(*) AS count FROM ${s}.open_loop_events`);
      return Number((res.rows[0] as Record<string, unknown>)?.["count"] ?? 0);
    } finally {
      client.release();
    }
  }
}
