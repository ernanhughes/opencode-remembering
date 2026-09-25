import type { ChunkRow, ScoredChunk } from "../storage";
import type { TemporalEnvelope } from "../temporal/model";
import type { StandingEvent } from "../trust/model";
import type { ContextTraceRecord } from "../trace/model";
import type { LoopEvent } from "../loops/model";
import type { MemoryActionRecord, MemoryRecord } from "../write/model";

/** Semantic storage ports. Domain logic depends on these, never on pg.Pool. */

export interface BaselineStorePort {
  readonly kind: string;
  initialise(embeddingDim: number): Promise<void>;
  checkDimension(embeddingDim: number): Promise<void>;
  upsertSource(sourceId: string, artifactType: string, contentHash: string, timestamp: string | null): Promise<boolean>;
  knownSources(): Promise<Record<string, string>>;
  removeSource(sourceId: string): Promise<number>;
  replaceChunks(sourceId: string, rows: ChunkRow[]): Promise<void>;
  lexicalSearch(query: string, k: number): Promise<ScoredChunk[]>;
  denseSearch(vector: number[], k: number, hnswProbe?: number): Promise<ScoredChunk[]>;
  ensureHnsw(): Promise<boolean>;
  stats(): Promise<Record<string, unknown>>;
  orphanChunks(): Promise<number>;
  embeddingVersions(): Promise<string[]>;
  /** True when the underlying chunks table/store exists. */
  isInitialised(): Promise<boolean>;
  readProjectMeta(): Promise<Record<string, string>>;
  writeProjectMeta(entries: Record<string, string>, overwrite?: boolean): Promise<void>;
  capabilities(): StoreCapabilities;
}

export interface TemporalStorePort {
  initialise(): Promise<void>;
  append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }>;
  list(): Promise<TemporalEnvelope[]>;
  count(): Promise<number>;
  subjects(): Promise<string[]>;
  versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }>;
}

export interface StandingStorePort {
  initialise(): Promise<void>;
  append(event: StandingEvent): Promise<{ duplicate: boolean }>;
  list(): Promise<StandingEvent[]>;
  count(): Promise<number>;
}

export interface TraceStorePort {
  initialise(): Promise<void>;
  put(trace: ContextTraceRecord): Promise<void>;
  get(traceId: string): Promise<ContextTraceRecord | null>;
  find(filters: {
    sourceId?: string; chunkId?: string; route?: string; workType?: string;
    terminalStage?: string; before?: string; after?: string; limit?: number;
  }): Promise<ContextTraceRecord[]>;
  count(): Promise<{ traces: number; oldest: string | null; newest: string | null }>;
}

export interface LoopStorePort {
  initialise(): Promise<void>;
  append(event: LoopEvent): Promise<{ duplicate: boolean }>;
  list(): Promise<LoopEvent[]>;
  count(): Promise<number>;
}

export interface WriteStorePort {
  initialise(): Promise<void>;
  putRecord(record: MemoryRecord): Promise<void>;
  getRecord(recordId: string): Promise<MemoryRecord | null>;
  listRecords?(): Promise<MemoryRecord[]>;
  updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void>;
  putAction(action: MemoryActionRecord): Promise<void>;
  getAction(actionId: string): Promise<MemoryActionRecord | null>;
  getActionByIdempotency(key: string): Promise<MemoryActionRecord | null>;
  history(recordId: string): Promise<MemoryActionRecord[]>;
  counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }>;
  /** Atomic multi-step write used by explicit-memory paths where available. */
  transaction?<T>(fn: () => Promise<T>): Promise<T>;
}

export type StoreCapabilities = {
  lexicalSearch: boolean;
  denseSearch: boolean;
  /** True only for a real vector index (pgvector/HNSW). JSON uses linear scan. */
  vectorIndex: boolean;
  transactions: boolean;
  tracePersistence: boolean;
  atomicWrite: boolean;
  remote: boolean;
  denseStrategy: "pgvector" | "linear-cosine" | "none";
};

export const POSTGRES_CAPABILITIES: StoreCapabilities = {
  lexicalSearch: true,
  denseSearch: true,
  vectorIndex: true,
  transactions: true,
  tracePersistence: true,
  atomicWrite: true,
  remote: false,
  denseStrategy: "pgvector",
};

export const JSON_CAPABILITIES: StoreCapabilities = {
  lexicalSearch: true,
  denseSearch: true,
  vectorIndex: false,
  transactions: false,
  tracePersistence: true,
  atomicWrite: true,
  remote: false,
  denseStrategy: "linear-cosine",
};

export const HTTP_CAPABILITIES: StoreCapabilities = {
  lexicalSearch: true,
  denseSearch: true,
  vectorIndex: true,
  transactions: true,
  tracePersistence: true,
  atomicWrite: true,
  remote: true,
  denseStrategy: "pgvector",
};

export type StorageBackendKind = "postgres" | "http" | "json";
