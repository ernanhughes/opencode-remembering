import type { Pool } from "pg";

import { ident } from "../db";
import { WRITE_STORE_VERSION, type MemoryActionRecord, type MemoryRecord } from "./model";

export interface WriteStore {
  initialise(): Promise<void>;
  putRecord(record: MemoryRecord): Promise<void>;
  getRecord(recordId: string): Promise<MemoryRecord | null>;
  updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void>;
  putAction(action: MemoryActionRecord): Promise<void>;
  getAction(actionId: string): Promise<MemoryActionRecord | null>;
  getActionByIdempotency(key: string): Promise<MemoryActionRecord | null>;
  history(recordId: string): Promise<MemoryActionRecord[]>;
  counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }>;
}

export class MemoryWriteStore implements WriteStore {
  records = new Map<string, MemoryRecord>();
  actions: MemoryActionRecord[] = [];
  async initialise(): Promise<void> {}
  async putRecord(record: MemoryRecord): Promise<void> {
    this.records.set(record.recordId, record);
  }
  async getRecord(recordId: string): Promise<MemoryRecord | null> {
    return this.records.get(recordId) ?? null;
  }
  async updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void> {
    const prior = this.records.get(recordId);
    if (prior) this.records.set(recordId, { ...prior, state });
  }
  async putAction(action: MemoryActionRecord): Promise<void> {
    this.actions.push(action);
  }
  async getAction(actionId: string): Promise<MemoryActionRecord | null> {
    return this.actions.find((a) => a.actionId === actionId) ?? null;
  }
  async getActionByIdempotency(key: string): Promise<MemoryActionRecord | null> {
    return this.actions.find((a) => a.idempotencyKey === key) ?? null;
  }
  async history(recordId: string): Promise<MemoryActionRecord[]> {
    return this.actions.filter((a) => a.recordId === recordId || a.targetRecordId === recordId);
  }
  async counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }> {
    const byAction: Record<string, number> = {};
    for (const a of this.actions) byAction[a.action] = (byAction[a.action] ?? 0) + 1;
    return {
      records: this.records.size, actions: this.actions.length, byAction,
      lastActionAt: this.actions.length ? (this.actions[this.actions.length - 1] as MemoryActionRecord).createdAt : null,
    };
  }
}

export class PostgresWriteStore implements WriteStore {
  constructor(private readonly pool: Pool, private readonly schema: string) {}
  async initialise(): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.memory_records (` +
          `record_id TEXT PRIMARY KEY, content TEXT NOT NULL, role TEXT NOT NULL, source_id TEXT NOT NULL, ` +
          `lineage_root TEXT NOT NULL, standing_ceiling TEXT NOT NULL, state TEXT NOT NULL, ` +
          `caller_scope TEXT NOT NULL, origin TEXT NOT NULL, evidence_refs JSONB NOT NULL DEFAULT '[]', ` +
          `event_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)`,
      );
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.memory_actions (` +
          `action_id TEXT PRIMARY KEY, action TEXT NOT NULL, record_id TEXT, target_record_id TEXT, ` +
          `reason TEXT NOT NULL DEFAULT '', caller_scope TEXT NOT NULL, origin TEXT NOT NULL, ` +
          `idempotency_key TEXT, relation JSONB, created_at TIMESTAMPTZ NOT NULL)`,
      );
      await client.query(`CREATE UNIQUE INDEX IF NOT EXISTS memory_actions_idem_idx ON ${s}.memory_actions (idempotency_key) WHERE idempotency_key IS NOT NULL`);
      await client.query(
        `INSERT INTO ${s}.meta (key, value) VALUES ('write.store_version', $1) ON CONFLICT (key) DO NOTHING`,
        [WRITE_STORE_VERSION],
      );
    } finally {
      client.release();
    }
  }
  async putRecord(record: MemoryRecord): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(
        `INSERT INTO ${s}.memory_records (record_id, content, role, source_id, lineage_root, standing_ceiling, state, caller_scope, origin, evidence_refs, event_time, effective_from, created_at) ` +
          `VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT (record_id) DO NOTHING`,
        [record.recordId, record.content, record.role, record.sourceId, record.lineageRoot, record.standingCeiling,
          record.state, record.callerScope, record.origin, JSON.stringify(record.evidenceRefs),
          record.eventTime, record.effectiveFrom, record.createdAt],
      );
    } finally {
      client.release();
    }
  }
  async getRecord(recordId: string): Promise<MemoryRecord | null> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT * FROM ${s}.memory_records WHERE record_id = $1`, [recordId]);
      if ((res.rowCount ?? 0) === 0) return null;
      const r = res.rows[0] as Record<string, unknown>;
      const refs = r["evidence_refs"];
      return {
        recordId: r["record_id"] as string, content: r["content"] as string, role: r["role"] as MemoryRecord["role"],
        sourceId: r["source_id"] as string, lineageRoot: r["lineage_root"] as string,
        standingCeiling: r["standing_ceiling"] as string, state: r["state"] as MemoryRecord["state"],
        callerScope: r["caller_scope"] as string, origin: r["origin"] as string,
        evidenceRefs: (typeof refs === "string" ? JSON.parse(refs) : refs) as string[],
        eventTime: new Date(r["event_time"] as string).toISOString(),
        effectiveFrom: new Date(r["effective_from"] as string).toISOString(),
        createdAt: new Date(r["created_at"] as string).toISOString(),
      };
    } finally {
      client.release();
    }
  }
  async updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`UPDATE ${s}.memory_records SET state = $1 WHERE record_id = $2`, [state, recordId]);
    } finally {
      client.release();
    }
  }
  async putAction(action: MemoryActionRecord): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(
        `INSERT INTO ${s}.memory_actions (action_id, action, record_id, target_record_id, reason, caller_scope, origin, idempotency_key, relation, created_at) ` +
          `VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (action_id) DO NOTHING`,
        [action.actionId, action.action, action.recordId, action.targetRecordId, action.reason,
          action.callerScope, action.origin, action.idempotencyKey,
          action.relation ? JSON.stringify(action.relation) : null, action.createdAt],
      );
    } finally {
      client.release();
    }
  }
  async getActionByIdempotency(key: string): Promise<MemoryActionRecord | null> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT * FROM ${s}.memory_actions WHERE idempotency_key = $1`, [key]);
      if ((res.rowCount ?? 0) === 0) return null;
      return this.rowToAction(res.rows[0] as Record<string, unknown>);
    } finally {
      client.release();
    }
  }
  async getAction(actionId: string): Promise<MemoryActionRecord | null> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT * FROM ${s}.memory_actions WHERE action_id = $1`, [actionId]);
      if ((res.rowCount ?? 0) === 0) return null;
      return this.rowToAction(res.rows[0] as Record<string, unknown>);
    } finally {
      client.release();
    }
  }
  async history(recordId: string): Promise<MemoryActionRecord[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(
        `SELECT * FROM ${s}.memory_actions WHERE record_id = $1 OR target_record_id = $1 ORDER BY created_at`,
        [recordId],
      );
      return res.rows.map((r) => this.rowToAction(r as Record<string, unknown>));
    } finally {
      client.release();
    }
  }
  async counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const records = await client.query(`SELECT count(*) AS count FROM ${s}.memory_records`);
      const actions = await client.query(`SELECT action, count(*) AS count, max(created_at) AS last FROM ${s}.memory_actions GROUP BY 1`);
      const byAction: Record<string, number> = {};
      let last: string | null = null;
      for (const r of actions.rows as Array<Record<string, unknown>>) {
        byAction[r["action"] as string] = Number(r["count"] ?? 0);
        if (r["last"]) last = new Date(r["last"] as string).toISOString();
      }
      return {
        records: Number((records.rows[0] as Record<string, unknown>)?.["count"] ?? 0),
        actions: actions.rows.reduce((n, r) => n + Number((r as Record<string, unknown>)["count"] ?? 0), 0),
        byAction, lastActionAt: last,
      };
    } finally {
      client.release();
    }
  }
  private rowToAction(r: Record<string, unknown>): MemoryActionRecord {
    const relation = r["relation"];
    return {
      actionId: r["action_id"] as string, action: r["action"] as MemoryActionRecord["action"],
      recordId: (r["record_id"] as string | null) ?? null,
      targetRecordId: (r["target_record_id"] as string | null) ?? null,
      reason: (r["reason"] as string) ?? "", callerScope: r["caller_scope"] as string,
      origin: r["origin"] as string, idempotencyKey: (r["idempotency_key"] as string | null) ?? null,
      relation: relation ? (typeof relation === "string" ? JSON.parse(relation) : relation) as MemoryActionRecord["relation"] : null,
      createdAt: new Date(r["created_at"] as string).toISOString(),
    };
  }
}
