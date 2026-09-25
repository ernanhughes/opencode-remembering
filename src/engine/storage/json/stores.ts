import * as path from "node:path";

import { EngineError } from "../../errors";
import { ENGINE_VERSION, SCHEMA_VERSION, type ChunkRow, type ScoredChunk } from "../../storage";
import { TEMPORAL_EVENT_SCHEMA, TEMPORAL_STORE_VERSION } from "../../temporal/model";
import { LOOP_STORE_VERSION } from "../../loops/model";
import { WRITE_STORE_VERSION } from "../../write/model";
import { STANDING_STORE_VERSION } from "../../trust/store";
import { TRACE_STORE_VERSION } from "../../trace/model";
import type {
  BaselineStorePort,
  LoopStorePort,
  StandingStorePort,
  StoreCapabilities,
  TemporalStorePort,
  TraceStorePort,
  WriteStorePort,
} from "../ports";
import { JSON_CAPABILITIES } from "../ports";
import type { TemporalEnvelope } from "../../temporal/model";
import type { StandingEvent } from "../../trust/model";
import type { ContextTraceRecord } from "../../trace/model";
import type { LoopEvent } from "../../loops/model";
import { validateLoopEvent } from "../../loops/model";
import type { MemoryActionRecord, MemoryRecord } from "../../write/model";
import { atomicWriteFile, appendJsonLine, ensureDir, readJsonFile, readJsonLines, withFileLock } from "./io";
import { denseRank, lexicalRank } from "./lexical";

export const JSON_STORE_FORMAT = "remembering-json-v1";

type MetaFile = { format: string; schema: string; projectPath: string; projectId: string; versions: Record<string, string>; createdAt: string };
type SourcesFile = { sources: Array<{ sourceId: string; artifactType: string; contentHash: string; timestamp: string | null }> };
type ChunksFile = { chunks: ChunkRow[] };

function storeDir(projectDir: string, configuredPath?: string): string {
  const rel = configuredPath ?? ".remembering/store";
  return path.isAbsolute(rel) ? rel : path.join(projectDir, rel);
}

export function jsonPaths(projectDir: string, configuredPath?: string) {
  const dir = storeDir(projectDir, configuredPath);
  return {
    dir,
    meta: path.join(dir, "meta.json"),
    sources: path.join(dir, "sources.json"),
    chunks: path.join(dir, "chunks.json"),
    temporal: path.join(dir, "temporal.jsonl"),
    standing: path.join(dir, "standing.jsonl"),
    traces: path.join(dir, "traces.jsonl"),
    loops: path.join(dir, "loops.jsonl"),
    records: path.join(dir, "memory-records.jsonl"),
    actions: path.join(dir, "memory-actions.jsonl"),
  };
}

async function loadMeta(projectDir: string, configuredPath?: string): Promise<MetaFile | null> {
  const p = jsonPaths(projectDir, configuredPath);
  return readJsonFile<MetaFile | null>(p.meta, null);
}

/** JSON baseline store: durable local fallback, lexical + linear-cosine retrieval. */
export class JsonBaselineStore implements BaselineStorePort {
  readonly kind = "json";
  private embeddingDim: number | null = null;

  constructor(
    private readonly projectDir: string,
    private readonly schema: string,
    private readonly projectPath: string,
    private readonly projectId: string,
    private readonly configuredPath?: string,
  ) {}

  private get paths() { return jsonPaths(this.projectDir, this.configuredPath); }

  capabilities(): StoreCapabilities { return JSON_CAPABILITIES; }

  async initialise(embeddingDim: number): Promise<void> {
    const dim = Math.floor(embeddingDim);
    if (!Number.isInteger(dim) || dim < 1 || dim > 65535) {
      throw new EngineError("CONFIG_INVALID", `refusing invalid embedding dimension ${JSON.stringify(embeddingDim)}.`);
    }
    await ensureDir(this.paths.dir);
    await withFileLock(this.paths.meta, async () => {
      const existing = await readJsonFile<MetaFile | null>(this.paths.meta, null);
      if (existing) {
        if (existing.format !== JSON_STORE_FORMAT) {
          throw new EngineError("STORE_VERSION_MISMATCH", `JSON store format ${JSON.stringify(existing.format)} is not ${JSON_STORE_FORMAT}; refusing to guess.`);
        }
        if (existing.projectPath !== this.projectPath) {
          throw new EngineError("STORE_PROJECT_MISMATCH", `JSON store is claimed by ${JSON.stringify(existing.projectPath)}, not ${JSON.stringify(this.projectPath)}. Refusing to mix projects.`);
        }
        if (existing.schema !== this.schema) {
          throw new EngineError("STORE_PROJECT_MISMATCH", `JSON store schema ${JSON.stringify(existing.schema)} does not match ${JSON.stringify(this.schema)}.`);
        }
        this.embeddingDim = dim;
        return;
      }
      const meta: MetaFile = {
        format: JSON_STORE_FORMAT, schema: this.schema, projectPath: this.projectPath,
        projectId: this.projectId, createdAt: new Date().toISOString(),
        versions: {
          engine: ENGINE_VERSION, schema: SCHEMA_VERSION, temporal: TEMPORAL_STORE_VERSION,
          temporalSchema: TEMPORAL_EVENT_SCHEMA, standing: STANDING_STORE_VERSION,
          trace: TRACE_STORE_VERSION, loops: LOOP_STORE_VERSION, write: WRITE_STORE_VERSION,
        },
      };
      await atomicWriteFile(this.paths.meta, JSON.stringify(meta, null, 2));
      await atomicWriteFile(this.paths.sources, JSON.stringify({ sources: [] }, null, 2));
      await atomicWriteFile(this.paths.chunks, JSON.stringify({ chunks: [] }, null, 2));
      this.embeddingDim = dim;
    });
    await this.checkDimension(dim);
  }

  async isInitialised(): Promise<boolean> {
    const meta = await loadMeta(this.projectDir, this.configuredPath);
    return meta !== null && meta.format === JSON_STORE_FORMAT;
  }

  async checkDimension(embeddingDim: number): Promise<void> {
    const data = await readJsonFile<ChunksFile>(this.paths.chunks, { chunks: [] });
    const first = data.chunks.find((c) => Array.isArray(c.embedding) && c.embedding.length > 0);
    if (first && first.embedding.length !== Math.floor(embeddingDim)) {
      throw new EngineError("DIMENSION_MISMATCH", `JSON store holds ${first.embedding.length}-dimensional vectors but configuration requests ${Math.floor(embeddingDim)}. Re-embed with a fresh store instead of mixing dimensions.`);
    }
    this.embeddingDim = Math.floor(embeddingDim);
  }

  async readProjectMeta(): Promise<Record<string, string>> {
    const meta = await loadMeta(this.projectDir, this.configuredPath);
    if (!meta) return {};
    return { "project.path": meta.projectPath, "project.schema": meta.schema, "project.id": meta.projectId };
  }

  async writeProjectMeta(entries: Record<string, string>, overwrite = false): Promise<void> {
    await withFileLock(this.paths.meta, async () => {
      const meta = await readJsonFile<MetaFile>(this.paths.meta, {
        format: JSON_STORE_FORMAT, schema: this.schema, projectPath: this.projectPath,
        projectId: this.projectId, createdAt: new Date().toISOString(), versions: {},
      });
      if (meta.format !== JSON_STORE_FORMAT) {
        throw new EngineError("STORE_VERSION_MISMATCH", `JSON store format mismatch.`);
      }
      const recorded = meta.projectPath;
      const incoming = entries["project.path"];
      if (recorded && incoming && recorded !== incoming && !overwrite) {
        throw new EngineError("STORE_PROJECT_MISMATCH", `JSON store is claimed by ${JSON.stringify(recorded)}, not ${JSON.stringify(incoming)}.`);
      }
      await atomicWriteFile(this.paths.meta, JSON.stringify(meta, null, 2));
    });
  }

  private async loadSources(): Promise<SourcesFile> {
    return readJsonFile<SourcesFile>(this.paths.sources, { sources: [] });
  }

  private async loadChunks(): Promise<ChunkRow[]> {
    const data = await readJsonFile<ChunksFile>(this.paths.chunks, { chunks: [] });
    return data.chunks;
  }

  async upsertSource(sourceId: string, artifactType: string, contentHash: string, timestamp: string | null): Promise<boolean> {
    return withFileLock(this.paths.sources, async () => {
      const data = await this.loadSources();
      const prior = data.sources.find((s) => s.sourceId === sourceId);
      if (prior && prior.contentHash === contentHash) return false;
      const next = data.sources.filter((s) => s.sourceId !== sourceId);
      next.push({ sourceId, artifactType, contentHash, timestamp });
      next.sort((a, b) => (a.sourceId < b.sourceId ? -1 : 1));
      await atomicWriteFile(this.paths.sources, JSON.stringify({ sources: next }, null, 2));
      return true;
    });
  }

  async knownSources(): Promise<Record<string, string>> {
    const data = await this.loadSources();
    const out: Record<string, string> = {};
    for (const s of data.sources) out[s.sourceId] = s.contentHash;
    return out;
  }

  async removeSource(sourceId: string): Promise<number> {
    let removed = 0;
    await withFileLock(this.paths.dir, async () => {
      const sources = await this.loadSources();
      const before = sources.sources.length;
      const next = sources.sources.filter((s) => s.sourceId !== sourceId);
      removed = before - next.length;
      if (removed > 0) await atomicWriteFile(this.paths.sources, JSON.stringify({ sources: next }, null, 2));
      const chunks = await this.loadChunks();
      const kept = chunks.filter((c) => c.sourceId !== sourceId);
      if (kept.length !== chunks.length) await atomicWriteFile(this.paths.chunks, JSON.stringify({ chunks: kept }, null, 2));
    });
    return removed;
  }

  async replaceChunks(sourceId: string, rows: ChunkRow[]): Promise<void> {
    await withFileLock(this.paths.chunks, async () => {
      const chunks = await this.loadChunks();
      const kept = chunks.filter((c) => c.sourceId !== sourceId);
      kept.push(...rows);
      await atomicWriteFile(this.paths.chunks, JSON.stringify({ chunks: kept }, null, 2));
    });
  }

  async lexicalSearch(query: string, k: number): Promise<ScoredChunk[]> {
    return lexicalRank(query, await this.loadChunks(), k);
  }

  async denseSearch(vector: number[], k: number): Promise<ScoredChunk[]> {
    return denseRank(vector, await this.loadChunks(), k);
  }

  async ensureHnsw(): Promise<boolean> { return false; }

  async stats(): Promise<Record<string, unknown>> {
    const sources = await this.loadSources();
    const chunks = await this.loadChunks();
    const versions = new Map<string, number>();
    for (const c of chunks) versions.set(c.embeddingVersion, (versions.get(c.embeddingVersion) ?? 0) + 1);
    return {
      sources: sources.sources.length, chunks: chunks.length,
      duplicate_chunks: chunks.length - new Set(chunks.map((c) => c.contentHash)).size,
      missing_vectors: chunks.filter((c) => !c.embedding || c.embedding.length === 0).length,
      distinct_embedding_versions: versions.size, distinct_chunkers: new Set(chunks.map((c) => c.chunker)).size,
      embedding_versions: [...versions.entries()].map(([version, n]) => ({ version, chunks: n })),
      fts_index: null, hnsw_index: null,
    };
  }

  async orphanChunks(): Promise<number> {
    const sources = new Set((await this.loadSources()).sources.map((s) => s.sourceId));
    return (await this.loadChunks()).filter((c) => !sources.has(c.sourceId)).length;
  }

  async embeddingVersions(): Promise<string[]> {
    return [...new Set((await this.loadChunks()).map((c) => c.embeddingVersion))];
  }
}

/** Append-only JSON event stores with atomic commit boundaries. */

export class JsonTemporalStore implements TemporalStorePort {
  constructor(private readonly projectDir: string, private readonly configuredPath?: string) {}
  private get file() { return jsonPaths(this.projectDir, this.configuredPath).temporal; }
  async initialise(): Promise<void> { await ensureDir(jsonPaths(this.projectDir, this.configuredPath).dir); }
  async append(envelope: TemporalEnvelope): Promise<{ duplicate: boolean }> {
    return withFileLock(this.file, async () => {
      const existing = await readJsonLines<TemporalEnvelope>(this.file);
      const prior = existing.find((e) => e.eventId === envelope.eventId);
      if (prior) {
        if (JSON.stringify(prior) !== JSON.stringify(envelope)) {
          throw new EngineError("TEMPORAL_CONFLICT", `conflicting reuse of event identity: ${envelope.eventId}.`);
        }
        return { duplicate: true };
      }
      await appendJsonLine(this.file, envelope);
      return { duplicate: false };
    });
  }
  async list(): Promise<TemporalEnvelope[]> { return readJsonLines<TemporalEnvelope>(this.file); }
  async count(): Promise<number> { return (await this.list()).length; }
  async subjects(): Promise<string[]> { return [...new Set((await this.list()).map((e) => e.subject))].sort(); }
  async versions(): Promise<{ storeVersion: string | null; eventSchemaVersion: string | null }> {
    return { storeVersion: TEMPORAL_STORE_VERSION, eventSchemaVersion: TEMPORAL_EVENT_SCHEMA };
  }
}

export class JsonStandingStore implements StandingStorePort {
  constructor(private readonly projectDir: string, private readonly configuredPath?: string) {}
  private get file() { return jsonPaths(this.projectDir, this.configuredPath).standing; }
  async initialise(): Promise<void> { await ensureDir(jsonPaths(this.projectDir, this.configuredPath).dir); }
  async append(event: StandingEvent): Promise<{ duplicate: boolean }> {
    return withFileLock(this.file, async () => {
      const existing = await readJsonLines<StandingEvent>(this.file);
      if (existing.some((e) => e.eventId === event.eventId)) return { duplicate: true };
      await appendJsonLine(this.file, event);
      return { duplicate: false };
    });
  }
  async list(): Promise<StandingEvent[]> { return readJsonLines<StandingEvent>(this.file); }
  async count(): Promise<number> { return (await this.list()).length; }
}

export class JsonTraceStore implements TraceStorePort {
  constructor(private readonly projectDir: string, private readonly configuredPath?: string) {}
  private get file() { return jsonPaths(this.projectDir, this.configuredPath).traces; }
  async initialise(): Promise<void> { await ensureDir(jsonPaths(this.projectDir, this.configuredPath).dir); }
  async put(trace: ContextTraceRecord): Promise<void> {
    await withFileLock(this.file, async () => {
      const existing = await readJsonLines<ContextTraceRecord>(this.file);
      if (existing.some((t) => t.traceId === trace.traceId)) return;
      await appendJsonLine(this.file, trace);
    });
  }
  async get(traceId: string): Promise<ContextTraceRecord | null> {
    return (await this.list()).find((t) => t.traceId === traceId) ?? null;
  }
  async find(filters: { sourceId?: string; chunkId?: string; route?: string; workType?: string; terminalStage?: string; before?: string; after?: string; limit?: number }): Promise<ContextTraceRecord[]> {
    let out = (await this.list()).sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    if (filters.route) out = out.filter((t) => t.route === filters.route);
    if (filters.workType) out = out.filter((t) => t.workType === filters.workType);
    if (filters.chunkId) out = out.filter((t) => t.candidates.some((c) => c.candidateId === filters.chunkId));
    if (filters.sourceId) out = out.filter((t) => t.candidates.some((c) => c.sourceId === filters.sourceId));
    if (filters.terminalStage) out = out.filter((t) => t.candidates.some((c) => c.terminalStage === filters.terminalStage));
    if (filters.before) out = out.filter((t) => t.createdAt < (filters.before as string));
    if (filters.after) out = out.filter((t) => t.createdAt > (filters.after as string));
    return out.slice(0, filters.limit ?? 20);
  }
  private async list(): Promise<ContextTraceRecord[]> { return readJsonLines<ContextTraceRecord>(this.file); }
  async count(): Promise<{ traces: number; oldest: string | null; newest: string | null }> {
    const all = (await this.list()).sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    return { traces: all.length, oldest: all[0]?.createdAt ?? null, newest: all[all.length - 1]?.createdAt ?? null };
  }
}

export class JsonLoopStore implements LoopStorePort {
  constructor(private readonly projectDir: string, private readonly configuredPath?: string) {}
  private get file() { return jsonPaths(this.projectDir, this.configuredPath).loops; }
  async initialise(): Promise<void> { await ensureDir(jsonPaths(this.projectDir, this.configuredPath).dir); }
  async append(event: LoopEvent): Promise<{ duplicate: boolean }> {
    return withFileLock(this.file, async () => {
      const existing = await readJsonLines<LoopEvent>(this.file);
      const prior = existing.find((e) => e.eventId === event.eventId);
      if (prior) {
        if (JSON.stringify(prior) !== JSON.stringify(event)) {
          throw new EngineError("LOOP_INVALID", `conflicting reuse of loop event identity: ${event.eventId}.`);
        }
        return { duplicate: true };
      }
      await appendJsonLine(this.file, event);
      return { duplicate: false };
    });
  }
  async list(): Promise<LoopEvent[]> {
    const raw = await readJsonLines<Record<string, unknown>>(this.file);
    return raw.map((r) => validateLoopEvent(r));
  }
  async count(): Promise<number> { return (await readJsonLines(this.file)).length; }
}

export class JsonWriteStore implements WriteStorePort {
  constructor(private readonly projectDir: string, private readonly configuredPath?: string) {}
  private get paths() { return jsonPaths(this.projectDir, this.configuredPath); }
  async initialise(): Promise<void> { await ensureDir(this.paths.dir); }
  private async readRecords(): Promise<MemoryRecord[]> { return readJsonLines<MemoryRecord>(this.paths.records); }
  private async readActions(): Promise<MemoryActionRecord[]> { return readJsonLines<MemoryActionRecord>(this.paths.actions); }
  /**
   * Atomic boundary: snapshot both files, run, restore on failure.
   * Uses a dedicated txn key so inner putRecord/putAction locks never deadlock.
   */
  async transaction<T>(fn: () => Promise<T>): Promise<T> {
    return withFileLock(`${this.paths.dir}:write-txn`, async () => {
      const { readFile, writeFile } = await import("node:fs/promises");
      const snapRecords = await readFile(this.paths.records, "utf8").catch(() => null);
      const snapActions = await readFile(this.paths.actions, "utf8").catch(() => null);
      try {
        return await fn();
      } catch (error) {
        try {
          if (snapRecords === null) await import("node:fs/promises").then((m) => m.rm(this.paths.records, { force: true }));
          else await writeFile(this.paths.records, snapRecords, "utf8");
          if (snapActions === null) await import("node:fs/promises").then((m) => m.rm(this.paths.actions, { force: true }));
          else await writeFile(this.paths.actions, snapActions, "utf8");
        } catch { /* restore best effort */ }
        throw error;
      }
    });
  }
  async putRecord(record: MemoryRecord): Promise<void> {
    await withFileLock(this.paths.records, async () => {
      const existing = await this.readRecords();
      if (existing.some((r) => r.recordId === record.recordId)) return;
      await appendJsonLine(this.paths.records, record);
    });
  }
  async getRecord(recordId: string): Promise<MemoryRecord | null> {
    return (await this.readRecords()).find((r) => r.recordId === recordId) ?? null;
  }
  async listRecords(): Promise<MemoryRecord[]> {
    return this.readRecords();
  }
  async updateRecordState(recordId: string, state: MemoryRecord["state"]): Promise<void> {
    // Append-only history is preserved in actions; record state is a
    // materialized projection rewritten atomically (temp+rename).
    await withFileLock(this.paths.records, async () => {
      const records = await this.readRecords();
      const next = records.map((r) => (r.recordId === recordId ? { ...r, state } : r));
      const tmp = `${this.paths.records}.rewrite.tmp`;
      const { writeFile, rename } = await import("node:fs/promises");
      await ensureDir(jsonPaths(this.projectDir, this.configuredPath).dir);
      await writeFile(tmp, next.map((r) => JSON.stringify(r)).join("\n") + (next.length ? "\n" : ""), "utf8");
      await rename(tmp, this.paths.records);
    });
  }
  async putAction(action: MemoryActionRecord): Promise<void> {
    await withFileLock(this.paths.actions, async () => {
      const existing = await this.readActions();
      if (existing.some((a) => a.actionId === action.actionId)) return;
      await appendJsonLine(this.paths.actions, action);
    });
  }
  async getAction(actionId: string): Promise<MemoryActionRecord | null> {
    return (await this.readActions()).find((a) => a.actionId === actionId) ?? null;
  }
  async getActionByIdempotency(key: string): Promise<MemoryActionRecord | null> {
    return (await this.readActions()).find((a) => a.idempotencyKey === key) ?? null;
  }
  async history(recordId: string): Promise<MemoryActionRecord[]> {
    return (await this.readActions()).filter((a) => a.recordId === recordId || a.targetRecordId === recordId);
  }
  async counts(): Promise<{ records: number; actions: number; byAction: Record<string, number>; lastActionAt: string | null }> {
    const records = await this.readRecords();
    const actions = await this.readActions();
    const byAction: Record<string, number> = {};
    for (const a of actions) byAction[a.action] = (byAction[a.action] ?? 0) + 1;
    return { records: records.length, actions: actions.length, byAction, lastActionAt: actions.length ? actions[actions.length - 1]!.createdAt : null };
  }
}
