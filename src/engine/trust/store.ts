import type { Pool } from "pg";

import { ident } from "../db";
import { TRUST_ENGINE_VERSION, type StandingEvent } from "./model";

export interface StandingEventStore {
  initialise(): Promise<void>;
  append(event: StandingEvent): Promise<{ duplicate: boolean }>;
  list(): Promise<StandingEvent[]>;
  count(): Promise<number>;
}

export class MemoryStandingStore implements StandingEventStore {
  private events = new Map<string, StandingEvent>();
  async initialise(): Promise<void> {}
  async append(event: StandingEvent): Promise<{ duplicate: boolean }> {
    if (this.events.has(event.eventId)) return { duplicate: true };
    this.events.set(event.eventId, event);
    return { duplicate: false };
  }
  async list(): Promise<StandingEvent[]> {
    return [...this.events.values()];
  }
  async count(): Promise<number> {
    return this.events.size;
  }
}

export const STANDING_STORE_VERSION = "standing-store-v0.1";

export class PostgresStandingStore implements StandingEventStore {
  constructor(private readonly pool: Pool, private readonly schema: string) {}
  async initialise(): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.standing_events (` +
          `event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, transition TEXT NOT NULL, ` +
          `level TEXT, reason TEXT NOT NULL DEFAULT '', at TIMESTAMPTZ NOT NULL)`,
      );
      await client.query(
        `INSERT INTO ${s}.meta (key, value) VALUES ('standing.store_version', $1) ON CONFLICT (key) DO NOTHING`,
        [STANDING_STORE_VERSION],
      );
    } finally {
      client.release();
    }
  }
  async append(event: StandingEvent): Promise<{ duplicate: boolean }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const existing = await client.query(`SELECT event_id FROM ${s}.standing_events WHERE event_id = $1`, [event.eventId]);
      if ((existing.rowCount ?? 0) > 0) return { duplicate: true };
      await client.query(
        `INSERT INTO ${s}.standing_events (event_id, source_id, transition, level, reason, at) VALUES ($1,$2,$3,$4,$5,$6)`,
        [event.eventId, event.sourceId, event.transition, event.level ?? null, event.reason, event.at],
      );
      return { duplicate: false };
    } finally {
      client.release();
    }
  }
  async list(): Promise<StandingEvent[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT event_id, source_id, transition, level, reason, at FROM ${s}.standing_events ORDER BY at, event_id`);
      return res.rows.map((r) => ({
        eventId: r.event_id, sourceId: r.source_id, transition: r.transition,
        level: r.level ?? undefined, reason: r.reason ?? "",
        at: new Date(r.at).toISOString(),
      }));
    } finally {
      client.release();
    }
  }
  async count(): Promise<number> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT count(*) AS count FROM ${s}.standing_events`);
      return Number((res.rows[0] as Record<string, unknown>)?.["count"] ?? 0);
    } finally {
      client.release();
    }
  }
}

export { TRUST_ENGINE_VERSION };
