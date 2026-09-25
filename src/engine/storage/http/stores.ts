import { EngineError } from "../../errors";
import type { ChunkRow, ScoredChunk } from "../../storage";
import type { TemporalEnvelope } from "../../temporal/model";
import type { StandingEvent } from "../../trust/model";
import type { ContextTraceRecord } from "../../trace/model";
import type { LoopEvent } from "../../loops/model";
import { validateLoopEvent } from "../../loops/model";
import type { MemoryActionRecord, MemoryRecord } from "../../write/model";
import type {
  BaselineStorePort, LoopStorePort, StandingStorePort, StoreCapabilities,
  TemporalStorePort, TraceStorePort, WriteStorePort,
} from "../ports";
import { HTTP_CAPABILITIES } from "../ports";

export type HttpBackendConfig = {
  url: string;
  /** Resolved bearer token (never logged). */
  token?: string;
  timeoutMs?: number;
};

function redactUrl(url: string): string {
  return url.replace(/([?&](token|key|secret)=)[^&]*/gi, "$1***");
}

async function rpc<T>(cfg: HttpBackendConfig, name: string, body: Record<string, unknown>): Promise<T> {
  const base = cfg.url.replace(/\/$/, "");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeoutMs ?? 15_000);
  try {
    const res = await fetch(`${base}/rpc/${name}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (res.status === 401 || res.status === 403) {
      throw new EngineError("HTTP_BACKEND_AUTH", `HTTP backend refused ${name} with status ${res.status}; check the configured token and project access.`);
    }
    if (!res.ok) {
      throw new EngineError("HTTP_BACKEND_PROTOCOL", `HTTP backend ${name} failed with status ${res.status} at ${redactUrl(base)}.`);
    }
    try {
      return (await res.json()) as T;
    } catch {
      throw new EngineError("HTTP_BACKEND_PROTOCOL", `HTTP backend ${name} returned malformed JSON.`);
    }
  } catch (error) {
    if (error instanceof EngineError) throw error;
    throw new EngineError(
      "HTTP_BACKEND_UNREACHABLE",
      `HTTP backend unreachable at ${redactUrl(cfg.url)}: ${error instanceof Error ? `${error.name}: ${error.message.slice(0, 160)}` : String(error).slice(0, 160)}`,
    );
  } finally {
    clearTimeout(timer);
  }
}

export async function checkHttpBackend(cfg: HttpBackendConfig, schema: string): Promise<void> {
  await rpc<{ ok: boolean }>(cfg, "remembering_health", { schema });
}

function toScored(rows: Array<Record<string, unknown>>): ScoredChunk[] {
  return rows.map((r, i) => ({
    chunkId: String(r["chunk_id"] ?? r["chunkId"]),
    sourceId: String(r["source_id"] ?? r["sourceId"]),
    text: String(r["text"] ?? ""),
    section: (r["section"] as string | null) ?? null,
    score: Number(r["score"] ?? 0),
    rank: Number(r["rank"] ?? i + 1),
  }));
}

export class HttpBaselineStore implements BaselineStorePort {
  readonly kind = "http";
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  capabilities(): StoreCapabilities { return HTTP_CAPABILITIES; }
  async initialise(embeddingDim: number): Promise<void> {
    await rpc(this.cfg, "remembering_initialise", { schema: this.schema, embedding_dim: Math.floor(embeddingDim) });
  }
  async checkDimension(embeddingDim: number): Promise<void> {
    await rpc(this.cfg, "remembering_check_dimension", { schema: this.schema, embedding_dim: Math.floor(embeddingDim) });
  }
  async upsertSource(sourceId: string, artifactType: string, contentHash: string, timestamp: string | null): Promise<boolean> {
    const out = await rpc<{ changed: boolean }>(this.cfg, "remembering_upsert_source", { schema: this.schema, source_id: sourceId, artifact_type: artifactType, content_hash: contentHash, timestamp });
    return out.changed;
  }
  async knownSources(): Promise<Record<string, string>> {
    const out = await rpc<{ sources: Array<{ source_id: string; content_hash: string }> }>(this.cfg, "remembering_known_sources", { schema: this.schema });
    const result: Record<string, string> = {};
    for (const s of out.sources) result[s.source_id] = s.content_hash;
    return result;
  }
  async removeSource(sourceId: string): Promise<number> {
    const out = await rpc<{ removed: number }>(this.cfg, "remembering_remove_source", { schema: this.schema, source_id: sourceId });
    return out.removed;
  }
  async replaceChunks(sourceId: string, rows: ChunkRow[]): Promise<void> {
    await rpc(this.cfg, "remembering_replace_chunks", {
      schema: this.schema, source_id: sourceId,
      chunks: rows.map((r) => ({ chunk_id: r.chunkId, source_id: r.sourceId, ordinal: r.ordinal, text: r.text, section: r.section, char_start: r.charStart, char_end: r.charEnd, content_hash: r.contentHash, chunker: r.chunker, embedding_version: r.embeddingVersion, embedding: r.embedding })),
    });
  }
  async lexicalSearch(query: string, k: number): Promise<ScoredChunk[]> {
    const out = await rpc<{ items: Array<Record<string, unknown>> }>(this.cfg, "remembering_lexical_search", { schema: this.schema, query, k });
    return toScored(out.items);
  }
  async denseSearch(vector: number[], k: number): Promise<ScoredChunk[]> {
    const out = await rpc<{ items: Array<Record<string, unknown>> }>(this.cfg, "remembering_dense_search", { schema: this.schema, query_vector: vector, k });
    return toScored(out.items);
  }
  async ensureHnsw(): Promise<boolean> {
    const out = await rpc<{ created: boolean }>(this.cfg, "remembering_ensure_hnsw", { schema: this.schema });
    return out.created;
  }
  async stats(): Promise<Record<string, unknown>> {
    return rpc<Record<string, unknown>>(this.cfg, "remembering_stats", { schema: this.schema });
  }
  async orphanChunks(): Promise<number> {
    const out = await rpc<{ orphans: number }>(this.cfg, "remembering_orphans", { schema: this.schema });
    return out.orphans;
  }
  async embeddingVersions(): Promise<string[]> {
    const out = await rpc<{ versions: string[] }>(this.cfg, "remembering_embedding_versions", { schema: this.schema });
    return out.versions;
  }
  async isInitialised(): Promise<boolean> {
    try {
      const out = await rpc<{ initialised: boolean }>(this.cfg, "remembering_is_initialised", { schema: this.schema });
      return out.initialised;
    } catch { return false; }
  }
  async readProjectMeta(): Promise<Record<string, string>> {
    const out = await rpc<{ meta: Record<string, string> }>(this.cfg, "remembering_read_meta", { schema: this.schema });
    return out.meta;
  }
  async writeProjectMeta(entries: Record<string, string>): Promise<void> {
    await rpc(this.cfg, "remembering_write_meta", { schema: this.schema, entries });
  }
}

export class HttpTemporalStore implements TemporalStorePort {
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  async initialise(): Promise<void> { await rpc(this.cfg, "remembering_temporal_initialise", { schema: this.schema }); }
  async append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }> {
    return rpc(this.cfg, "remembering_temporal_append", { schema: this.schema, envelope });
  }
  async list(): Promise<TemporalEnvelope[]> {
    const out = await rpc<{ events: TemporalEnvelope[] }>(this.cfg, "remembering_temporal_list", { schema: this.schema });
    return out.events;
  }
  async count(): Promise<number> {
    const out = await rpc<{ count: number }>(this.cfg, "remembering_temporal_count", { schema: this.schema });
    return out.count;
  }
  async subjects(): Promise<string[]> {
    const out = await rpc<{ subjects: string[] }>(this.cfg, "remembering_temporal_subjects", { schema: this.schema });
    return out.subjects;
  }
  async versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }> {
    return rpc(this.cfg, "remembering_temporal_versions", { schema: this.schema });
  }
}

export class HttpStandingStore implements StandingStorePort {
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  async initialise(): Promise<void> { await rpc(this.cfg, "remembering_standing_initialise", { schema: this.schema }); }
  async append(event: StandingEvent): Promise<{ duplicate: boolean }> {
    return rpc(this.cfg, "remembering_standing_append", { schema: this.schema, event });
  }
  async list(): Promise<StandingEvent[]> {
    const out = await rpc<{ events: StandingEvent[] }>(this.cfg, "remembering_standing_list", { schema: this.schema });
    return out.events;
  }
  async count(): Promise<number> {
    const out = await rpc<{ count: number }>(this.cfg, "remembering_standing_count", { schema: this.schema });
    return out.count;
  }
}

export class HttpTraceStore implements TraceStorePort {
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  async initialise(): Promise<void> { await rpc(this.cfg, "remembering_trace_initialise", { schema: this.schema }); }
  async put(trace: ContextTraceRecord): Promise<void> {
    await rpc(this.cfg, "remembering_trace_put", { schema: this.schema, trace });
  }
  async get(traceId: string): Promise<ContextTraceRecord | null> {
    const out = await rpc<{ trace: ContextTraceRecord | null }>(this.cfg, "remembering_trace_get", { schema: this.schema, trace_id: traceId });
    return out.trace;
  }
  async find(filters: { sourceId?: string; chunkId?: string; route?: string; workType?: string; terminalStage?: string; before?: string; after?: string; limit?: number }): Promise<ContextTraceRecord[]> {
    const out = await rpc<{ traces: ContextTraceRecord[] }>(this.cfg, "remembering_trace_find", { schema: this.schema, filters });
    return out.traces;
  }
  async count(): Promise<{ traces: number; oldest: string | null; newest: string | null }> {
    return rpc(this.cfg, "remembering_trace_count", { schema: this.schema });
  }
}

export class HttpLoopStore implements LoopStorePort {
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  async initialise(): Promise<void> { await rpc(this.cfg, "remembering_loop_initialise", { schema: this.schema }); }
  async append(event: LoopEvent): Promise<{ duplicate: boolean }> {
    return rpc(this.cfg, "remembering_loop_append", { schema: this.schema, event });
  }
  async list(): Promise<LoopEvent[]> {
    const out = await rpc<{ events: Array<Record<string, unknown>> }>(this.cfg, "remembering_loop_list", { schema: this.schema });
    return out.events.map((e) => validateLoopEvent(e));
  }
  async count(): Promise<number> {
    const out = await rpc<{ count: number }>(this.cfg, "remembering_loop_count", { schema: this.schema });
    return out.count;
  }
}

export class HttpWriteStore implements WriteStorePort {
  constructor(private readonly cfg: HttpBackendConfig, private readonly schema: string) {}
  async initialise(): Promise<void> { await rpc(this.cfg, "remembering_write_initialise", { schema: this.schema }); }
  async putRecord(record: MemoryRecord): Promise<void> { await rpc(this.cfg, "remembering_write_put_record", { schema: this.schema, record }); }
  async getRecord(recordId: string): Promise<MemoryRecord | null> {
    const out = await rpc<{ record: MemoryRecord | null }>(this.cfg, "remembering_write_get_record", { schema: this.schema, record_id: recordId });
    return out.record;
  }
  async listRecords(): Promise<MemoryRecord[]> {
    try {
      const out = await rpc<{ records: MemoryRecord[] }>(this.cfg, "remembering_write_list_records", { schema: this.schema });
      return out.records;
    } catch { return []; }
  }
  async updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void> {
    await rpc(this.cfg, "remembering_write_update_state", { schema: this.schema, record_id: recordId, state });
  }
  async putAction(action: MemoryActionRecord): Promise<void> { await rpc(this.cfg, "remembering_write_put_action", { schema: this.schema, action }); }
  async getAction(actionId: string): Promise<MemoryActionRecord | null> {
    const out = await rpc<{ action: MemoryActionRecord | null }>(this.cfg, "remembering_write_get_action", { schema: this.schema, action_id: actionId });
    return out.action;
  }
  async getActionByIdempotency(key: string): Promise<MemoryActionRecord | null> {
    const out = await rpc<{ action: MemoryActionRecord | null }>(this.cfg, "remembering_write_get_idem", { schema: this.schema, key });
    return out.action;
  }
  async history(recordId: string): Promise<MemoryActionRecord[]> {
    const out = await rpc<{ history: MemoryActionRecord[] }>(this.cfg, "remembering_write_history", { schema: this.schema, record_id: recordId });
    return out.history;
  }
  async counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }> {
    return rpc(this.cfg, "remembering_write_counts", { schema: this.schema });
  }
}
