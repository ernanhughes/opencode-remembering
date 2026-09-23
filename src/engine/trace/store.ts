import type { Pool } from "pg";

import { ident } from "../db";
import { EngineError } from "../errors";
import type { ContextTraceRecord } from "./model";

export interface TraceStore {
  initialise(): Promise<void>;
  put(trace: ContextTraceRecord): Promise<void>;
  get(traceId: string): Promise<ContextTraceRecord | null>;
  find(filters: {
    sourceId?: string;
    chunkId?: string;
    route?: string;
    workType?: string;
    terminalStage?: string;
    before?: string;
    after?: string;
    limit?: number;
  }): Promise<ContextTraceRecord[]>;
  count(): Promise<{ traces: number; oldest: string | null; newest: string | null }>;
}

export class MemoryTraceStore implements TraceStore {
  private traces = new Map<string, ContextTraceRecord>();
  async initialise(): Promise<void> {}
  async put(trace: ContextTraceRecord): Promise<void> {
    this.traces.set(trace.traceId, trace);
  }
  async get(traceId: string): Promise<ContextTraceRecord | null> {
    return this.traces.get(traceId) ?? null;
  }
  async find(filters: {
    sourceId?: string; chunkId?: string; route?: string; workType?: string;
    terminalStage?: string; before?: string; after?: string; limit?: number;
  }): Promise<ContextTraceRecord[]> {
    let out = [...this.traces.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    if (filters.route) out = out.filter((t) => t.route === filters.route);
    if (filters.workType) out = out.filter((t) => t.workType === filters.workType);
    if (filters.chunkId) out = out.filter((t) => t.candidates.some((c) => c.candidateId === filters.chunkId));
    if (filters.sourceId) out = out.filter((t) => t.candidates.some((c) => c.sourceId === filters.sourceId));
    if (filters.terminalStage) out = out.filter((t) => t.candidates.some((c) => c.terminalStage === filters.terminalStage));
    if (filters.before) out = out.filter((t) => t.createdAt < (filters.before as string));
    if (filters.after) out = out.filter((t) => t.createdAt > (filters.after as string));
    return out.slice(0, filters.limit ?? 20);
  }
  async count(): Promise<{ traces: number; oldest: string | null; newest: string | null }> {
    const all = [...this.traces.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    return {
      traces: all.length,
      oldest: all[0]?.createdAt ?? null,
      newest: all[all.length - 1]?.createdAt ?? null,
    };
  }
}

export class PostgresTraceStore implements TraceStore {
  constructor(private readonly pool: Pool, private readonly schema: string) {}
  async initialise(): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.context_traces (` +
          `trace_id TEXT PRIMARY KEY, query TEXT NOT NULL, route TEXT NOT NULL, ` +
          `work_type TEXT, created_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL, digest TEXT NOT NULL)`,
      );
      await client.query(`CREATE INDEX IF NOT EXISTS context_traces_created_idx ON ${s}.context_traces (created_at)`);
    } finally {
      client.release();
    }
  }
  async put(trace: ContextTraceRecord): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(
        `INSERT INTO ${s}.context_traces (trace_id, query, route, work_type, created_at, body, digest) ` +
          `VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (trace_id) DO NOTHING`,
        [trace.traceId, trace.query, trace.route, trace.workType, trace.createdAt, JSON.stringify(trace), trace.digest],
      );
    } finally {
      client.release();
    }
  }
  async get(traceId: string): Promise<ContextTraceRecord | null> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT body FROM ${s}.context_traces WHERE trace_id = $1`, [traceId]);
      if ((res.rowCount ?? 0) === 0) return null;
      const body = (res.rows[0] as Record<string, unknown>)?.["body"];
      return (typeof body === "string" ? JSON.parse(body) : body) as ContextTraceRecord;
    } finally {
      client.release();
    }
  }
  async find(filters: {
    sourceId?: string; chunkId?: string; route?: string; workType?: string;
    terminalStage?: string; before?: string; after?: string; limit?: number;
  }): Promise<ContextTraceRecord[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const clauses: string[] = [];
      const params: unknown[] = [];
      if (filters.route) { params.push(filters.route); clauses.push(`route = $${params.length}`); }
      if (filters.workType) { params.push(filters.workType); clauses.push(`work_type = $${params.length}`); }
      if (filters.before) { params.push(filters.before); clauses.push(`created_at < $${params.length}`); }
      if (filters.after) { params.push(filters.after); clauses.push(`created_at > $${params.length}`); }
      const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
      const res = await client.query(
        `SELECT body FROM ${s}.context_traces ${where} ORDER BY created_at LIMIT $${params.length + 1}`,
        [...params, Math.min(filters.limit ?? 20, 100)],
      );
      let out = res.rows.map((r) => {
        const body = (r as Record<string, unknown>)["body"];
        return (typeof body === "string" ? JSON.parse(body) : body) as ContextTraceRecord;
      });
      // JSON-contained filters are applied in TS to keep SQL simple and safe.
      if (filters.chunkId) out = out.filter((t) => t.candidates.some((c) => c.candidateId === filters.chunkId));
      if (filters.sourceId) out = out.filter((t) => t.candidates.some((c) => c.sourceId === filters.sourceId));
      if (filters.terminalStage) out = out.filter((t) => t.candidates.some((c) => c.terminalStage === filters.terminalStage));
      return out;
    } finally {
      client.release();
    }
  }
  async count(): Promise<{ traces: number; oldest: string | null; newest: string | null }> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT count(*) AS count, min(created_at) AS oldest, max(created_at) AS newest FROM ${s}.context_traces`);
      const row = res.rows[0] as Record<string, unknown>;
      return {
        traces: Number(row["count"] ?? 0),
        oldest: row["oldest"] ? new Date(row["oldest"] as string).toISOString() : null,
        newest: row["newest"] ? new Date(row["newest"] as string).toISOString() : null,
      };
    } finally {
      client.release();
    }
  }
}

export function requireTrace(trace: ContextTraceRecord | null, traceId: string): ContextTraceRecord {
  if (!trace) throw new EngineError("TRACE_NOT_FOUND", `no trace ${JSON.stringify(traceId)}.`);
  return trace;
}
