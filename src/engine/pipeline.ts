import { Pool } from "pg";

import { resolveRoute, runPipelineStages, type MemoryRoute, type RouteRequest } from "./context";
import { connectPool, ensureExtension, ident } from "./db";
import { canonicalProjectDir, doctorBaseline } from "./doctor";
import { buildEmbedder, type EmbeddingProvider, type EmbeddingSpec } from "./embeddings";
import { assertSchemaName, EngineError } from "./errors";
import { establishFrame, frameHealth } from "./frame/service";
import { chunkSource, discover, parseFile, sha1, type ChunkingConfig, DEFAULT_CHUNKING } from "./ingest";
import { MemoryLoopStore, PostgresLoopStore } from "./loops/store";
import { reduceLoopEvents, validateLoopEvent, type LoopState } from "./loops/model";
import { Retriever, type RetrievalConfig, type RetrievalTrace } from "./retrieval";
import { BUDGET_VERSION, PROVENANCE_VERSION, REDUNDANCY_VERSION, SELECT_POLICY_VERSION } from "./selection/model";
import { BaselineStore, readProjectMeta } from "./storage";
import { validateStandpoint } from "./temporal/model";
import { reduceTemporalLog, resolveAtStandpoint } from "./temporal/reducer";
import { MemoryTemporalStore, PostgresTemporalStore } from "./temporal/store";
import { TemporalService } from "./temporal/service";
import { createTrace, TRACE_ENGINE_VERSION, TRACE_SCHEMA_VERSION, TRACE_STORE_VERSION, REPLAY_PROTOCOL_VERSION, verifyTrace } from "./trace/model";
import { MemoryTraceStore, PostgresTraceStore } from "./trace/store";
import { TraceService } from "./trace/service";
import { FRAMING_ENGINE_VERSION } from "./frame/model";
import { INSTRUCTION_SCREEN_VERSION, TRUST_ENGINE_VERSION, type TrustLevel } from "./trust/model";
import { builtinTrustPolicy, loadTrustPolicy } from "./trust/policy";
import { resolveStanding, validateStandingEvent } from "./trust/standing";
import { MemoryStandingStore, PostgresStandingStore, STANDING_STORE_VERSION } from "./trust/store";
import { LOOP_CLOSURE_VERSION, LOOP_ENGINE_VERSION, LOOP_EVENT_SCHEMA, LOOP_REDUCER_VERSION, LOOP_STORE_VERSION } from "./loops/model";
import { TEMPORAL_EVENT_SCHEMA, TEMPORAL_REDUCER_VERSION, TEMPORAL_STORE_VERSION } from "./temporal/model";
import { builtinWritePolicy, loadWritePolicy } from "./write/policy";
import { WriteService, type WriteInput } from "./write/service";
import { MemoryWriteStore, PostgresWriteStore } from "./write/store";
import { ACTION_SCHEMA_VERSION, RECORD_SCHEMA_VERSION, RELATION_VERSION, WRITE_ENGINE_VERSION, WRITE_STORE_VERSION } from "./write/model";

export type EngineOptions = {
  dsn: string;
  schema: string;
  projectDirectory: string;
  embedding: EmbeddingSpec;
  retrieval: RetrievalConfig;
  chunking?: ChunkingConfig;
};

export type RefreshReport = {
  ok: boolean;
  schema: string;
  discovered: number;
  indexed: number;
  unchanged: number;
  added: number;
  changed: number;
  removed: number;
  chunks: number;
  embedded: number;
  failed: string[];
  temporal?: { imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] };
  message?: string;
};

export type SearchItem = {
  chunk_id: string;
  source_id: string;
  section: string | null;
  rank: number;
  score: number;
  text: string;
  lexical_rank: number | null;
  dense_rank: number | null;
  stage: string;
};

export type SearchResult = {
  ok: boolean;
  indexed: boolean;
  schema: string;
  items: SearchItem[];
  trace: {
    query: string;
    mode: string;
    lexical_count: number;
    dense_count: number;
    fused_count: number;
    reranked_count: number;
    lexical_ids: string[];
    dense_ids: string[];
    fused_ids: string[];
    reranked_ids: string[];
    latencies_ms: Record<string, number>;
    embedding: { provider: string; model: string; dimension: number; version: string };
    retrieval: {
      mode: string;
      lexical_k: number;
      dense_k: number;
      fusion_k: number;
      rerank_k: number;
      reranker: string;
    };
    dense_executed: boolean;
  } | null;
  message?: string;
};

export const NOT_INDEXED_MESSAGE =
  "This project's remembering schema has not been indexed yet. Run memory_setup to initialise the schema and ingest the repository.";

/**
 * Native TypeScript baseline engine (Stage 2 vertical slice).
 * Owns its pg Pool; call close() when done.
 */
export class RememberingEngine {
  private pool: Pool | null = null;
  private embedder: EmbeddingProvider;
  readonly chunking: ChunkingConfig;

  constructor(private readonly options: EngineOptions) {
    assertSchemaName(options.schema);
    this.embedder = buildEmbedder(options.embedding);
    this.chunking = options.chunking ?? DEFAULT_CHUNKING;
  }

  /** Override the embedder (tests / differential harness). */
  withEmbedder(embedder: EmbeddingProvider): this {
    this.embedder = embedder;
    return this;
  }

  private async poolConnected(): Promise<Pool> {
    if (!this.pool) this.pool = await connectPool(this.options.dsn);
    return this.pool;
  }

  async close(): Promise<void> {
    if (this.pool) {
      await this.pool.end().catch(() => {});
      this.pool = null;
    }
  }

  private store(pool: Pool): BaselineStore {
    return new BaselineStore(pool, this.options.dsn, this.options.schema);
  }

  async doctor(): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const baseline = await doctorBaseline(pool, {
      dsn: this.options.dsn,
      schema: this.options.schema,
      projectDirectory: this.options.projectDirectory,
      embedding: this.options.embedding,
    });
    return { ...baseline, native: true };
  }

  async setup(): Promise<{ ok: boolean; schema: string; embedding: { provider: string; model: string; dimension: number; version: string } }> {
    const pool = await this.poolConnected();
    const client = await pool.connect();
    try {
      await ensureExtension(client, "vector");
      await ensureExtension(client, "pg_trgm");
    } finally {
      client.release();
    }
    const probe = await this.embedder.embed(["dimension probe"]);
    const dimension = probe.dimension;
    const store = this.store(pool);
    await store.initialise(dimension);
    await this.ensureProjectMeta(pool);
    await store.ensureHnsw();
    // Stage 3 subsystem stores (idempotent; no data migration).
    await new PostgresTemporalStore(pool, this.options.schema).initialise();
    await new PostgresStandingStore(pool, this.options.schema).initialise();
    await new PostgresTraceStore(pool, this.options.schema).initialise();
    await new PostgresLoopStore(pool, this.options.schema).initialise();
    await new PostgresWriteStore(pool, this.options.schema).initialise();
    const { error: trustError } = await loadTrustPolicy(this.options.projectDirectory);
    if (trustError) throw new EngineError("TRUST_POLICY_INVALID", trustError);
    const { valid: writeValid, error: writeError } = await loadWritePolicy(this.options.projectDirectory);
    if (!writeValid && writeError) throw new EngineError("WRITE_POLICY_INVALID", writeError);
    return {
      ok: true,
      schema: this.options.schema,
      embedding: {
        provider: probe.provider,
        model: probe.model,
        dimension: probe.dimension,
        version: `${probe.provider}:${probe.model}:${probe.dimension}`,
      },
    };
  }

  private async ensureProjectMeta(pool: Pool): Promise<void> {
    const canonical = await canonicalProjectDir(this.options.projectDirectory);
    const client = await pool.connect();
    try {
      const meta = await readProjectMeta(client, this.options.schema);
      const recorded = meta["project.path"];
      if (recorded != null && recorded !== canonical) {
        throw new EngineError(
          "SCHEMA_MISMATCH",
          `schema ${JSON.stringify(this.options.schema)} is already claimed by project ${JSON.stringify(recorded)}, but this project resolves to ${JSON.stringify(canonical)}. Refusing to mix projects: fix the schema configuration instead of sharing an index.`,
        );
      }
      const stamp = new Date().toISOString();
      const s = ident(this.options.schema);
      for (const [key, value] of [
        ["project.path", canonical],
        ["project.schema", this.options.schema],
        ["project.recorded_at", stamp],
      ] as Array<[string, string]>) {
        await client.query(
          `INSERT INTO ${s}.meta (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING`,
          [key, value],
        );
      }
    } finally {
      client.release();
    }
  }

  async refresh(): Promise<RefreshReport> {
    const pool = await this.poolConnected();
    const store = this.store(pool);
    // Baseline refresh requires an initialised store.
    const probe = await this.embedder.embed(["dimension probe"]);
    await store.initialise(probe.dimension);
    await this.ensureProjectMeta(pool);

    const report: RefreshReport = {
      ok: true,
      schema: this.options.schema,
      discovered: 0,
      indexed: 0,
      unchanged: 0,
      added: 0,
      changed: 0,
      removed: 0,
      chunks: 0,
      embedded: 0,
      failed: [],
    };
    const { realpath } = await import("node:fs/promises");
    let root: string;
    try {
      root = await realpath(this.options.projectDirectory);
    } catch {
      const { resolve } = await import("node:path");
      root = resolve(this.options.projectDirectory);
    }
    const paths = await discover(root);
    report.discovered = paths.length;
    const known = await store.knownSources();
    const seen = new Set<string>();
    const version = await this.embeddingVersion();
    for (const fullPath of paths) {
      const source = await parseFile(fullPath, root);
      if (!source) {
        const { basename } = await import("node:path");
        report.failed.push(basename(fullPath));
        continue;
      }
      seen.add(source.sourceId);
      if (known[source.sourceId] === source.contentHash) {
        report.unchanged += 1;
        continue;
      }
      const isNew = !(source.sourceId in known);
      await store.upsertSource(source.sourceId, source.artifactType, source.contentHash, source.timestamp);
      const chunks = chunkSource(source, this.chunking);
      const texts = chunks.map((c) => c.text);
      const vectors = texts.length ? (await this.embedder.embed(texts)).vectors : [];
      const rows = chunks.map((c, i) => ({
        chunkId: c.chunkId,
        sourceId: c.sourceId,
        ordinal: c.ordinal,
        text: c.text,
        section: c.section,
        charStart: c.charStart,
        charEnd: c.charEnd,
        contentHash: c.contentHash,
        chunker: `${this.chunking.policy}:${this.chunking.chunkerVersion}`,
        embeddingVersion: version,
        embedding: vectors[i] ?? [],
      }));
      await store.replaceChunks(source.sourceId, rows);
      report.chunks += rows.length;
      report.embedded += rows.length;
      report.indexed += 1;
      if (isNew) report.added += 1;
      else report.changed += 1;
    }
    for (const sourceId of Object.keys(known).sort()) {
      if (seen.has(sourceId)) continue;
      // Explicit-memory sources are managed by the write subsystem,
      // not by repository discovery: refresh must never prune them.
      if (sourceId.startsWith("memory://")) continue;
      await store.removeSource(sourceId);
      report.removed += 1;
    }
    await store.ensureHnsw();
    await this.verifyWith(store);
    const temporal = await this.temporalImportSafe();
    const report2 = report as RefreshReport & { temporal?: { imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] }; message?: string };
    report2.temporal = {
      imported: temporal.imported,
      duplicates: temporal.duplicates,
      failed: temporal.failed,
      events: temporal.events,
      subjects: temporal.subjects,
    };
    if (temporal.failed.length > 0) {
      report2.message = `temporal import failures: ${temporal.failed.join("; ")}`;
    }
    // Standing / loop / explicit-memory file imports run on every refresh so
    // checked-in event files converge without a separate manual step.
    // Failures are recorded, never thrown: refresh must not fail because an
    // optional event file has a bad line.
    try {
      await this.trustImport();
    } catch {
      /* recorded in trust health */
    }
    try {
      await this.loopImport();
    } catch {
      /* recorded in loops health */
    }
    try {
      await this.memoryImport();
    } catch {
      /* recorded in writes health */
    }
    return report;
  }

  private async temporalImportSafe(): Promise<{ imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] }> {
    try {
      const pool = await this.poolConnected();
      const pgStore = new PostgresTemporalStore(pool, this.options.schema);
      await pgStore.initialise();
      const { TemporalService: Service } = await import("./temporal/service");
      const summary = await new Service(pgStore).importFile(this.options.projectDirectory);
      return summary;
    } catch {
      return { imported: 0, duplicates: 0, failed: [], events: 0, subjects: [] };
    }
  }

  private async embeddingVersion(): Promise<string> {
    // Version without dimension probe side effects: derive from spec.
    // The stored per-chunk version is provider:model:dimension after first embed.
    const probe = await this.embedder.embed(["dimension probe"]);
    return `${probe.provider}:${probe.model}:${probe.dimension}`;
  }

  async verify(): Promise<void> {
    const pool = await this.poolConnected();
    await this.verifyWith(this.store(pool));
  }

  private async verifyWith(store: BaselineStore): Promise<void> {
    if (await store.orphanChunks()) {
      throw new EngineError("VERIFY_FAILED", "orphan chunks remain after refresh");
    }
    const versions = await store.embeddingVersions();
    if (versions.length > 1) {
      throw new EngineError(
        "VERIFY_FAILED",
        `mixed embedding versions in store: ${JSON.stringify(versions.sort())}`,
      );
    }
  }

  async search(query: string, limit = 8): Promise<SearchResult> {
    const trimmed = query.trim();
    if (!trimmed) {
      return { ok: true, indexed: true, schema: this.options.schema, items: [], trace: null, message: "empty query: no retrieval attempted." };
    }
    const boundedLimit = Math.max(1, Math.min(limit, 20));
    const pool = await this.poolConnected();
    const client = await pool.connect();
    try {
      const reg = await client.query("SELECT to_regclass($1)", [`${this.options.schema}.chunks`]);
      if (reg.rows[0]?.to_regclass == null) {
        return { ok: true, indexed: false, schema: this.options.schema, items: [], trace: null, message: NOT_INDEXED_MESSAGE };
      }
    } finally {
      client.release();
    }
    const probe = await this.embedder.embed(["dimension probe"]);
    const embIdentity = {
      provider: probe.provider,
      model: probe.model,
      dimension: probe.dimension,
      version: `${probe.provider}:${probe.model}:${probe.dimension}`,
    };
    const retriever = new Retriever(this.store(pool), this.embedder, this.options.retrieval);
    const trace: RetrievalTrace = await retriever.retrieve(trimmed);
    const lexicalRanks = new Map(trace.lexical.map((c) => [c.chunkId, c.rank]));
    const denseRanks = new Map(trace.dense.map((c) => [c.chunkId, c.rank]));
    const finalStage = this.options.retrieval.reranker !== "none" ? "reranked" : "fused";
    const items: SearchItem[] = trace.reranked.slice(0, boundedLimit).map((c) => ({
      chunk_id: c.chunkId,
      source_id: c.sourceId,
      section: c.section,
      rank: c.rank,
      score: c.score,
      text: c.text,
      lexical_rank: lexicalRanks.get(c.chunkId) ?? null,
      dense_rank: denseRanks.get(c.chunkId) ?? null,
      stage: finalStage,
    }));
    return {
      ok: true,
      indexed: true,
      schema: this.options.schema,
      items,
      trace: {
        query: trace.query,
        mode: this.options.retrieval.mode,
        lexical_count: trace.lexical.length,
        dense_count: trace.dense.length,
        fused_count: trace.fused.length,
        reranked_count: trace.reranked.length,
        lexical_ids: trace.lexical.map((c) => c.chunkId),
        dense_ids: trace.dense.map((c) => c.chunkId),
        fused_ids: trace.fused.map((c) => c.chunkId),
        reranked_ids: trace.reranked.map((c) => c.chunkId),
        latencies_ms: Object.fromEntries(
          Object.entries(trace.latenciesMs).map(([k, v]) => [k, Math.round(v * 100) / 100]),
        ),
        embedding: embIdentity,
        retrieval: {
          mode: this.options.retrieval.mode,
          lexical_k: this.options.retrieval.lexicalK,
          dense_k: this.options.retrieval.denseK,
          fusion_k: this.options.retrieval.fusionK,
          rerank_k: this.options.retrieval.rerankK,
          reranker: this.options.retrieval.reranker,
        },
        dense_executed: "dense" in trace.latenciesMs,
      },
    };
  }

  // -- Stage 3A: temporal ---------------------------------------------

  async temporalImport(): Promise<{ ok: boolean; schema: string; message: string; imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] }> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresTemporalStore(pool, this.options.schema);
    await pgStore.initialise();
    const service = new TemporalService(pgStore);
    const summary = await service.importFile(this.options.projectDirectory);
    return {
      ok: summary.failed.length === 0,
      schema: this.options.schema,
      message: summary.failed.length
        ? `temporal import failures: ${summary.failed.join("; ")}`
        : `temporal events +${summary.imported} (${summary.events} total).`,
      ...summary,
    };
  }

  async state(subject: string, options: { query?: string; route?: RouteRequest; temporal?: { mode?: string; valid_at?: string; known_at?: string } } = {}): Promise<Record<string, unknown>> {
    if (!subject.trim()) throw new EngineError("TEMPORAL_EVENT_INVALID", "subject is required.");
    const route = resolveRoute(options.query ?? "", options.route ?? "auto");
    const standpoint = validateStandpoint({
      mode: options.temporal?.mode ?? "current",
      valid_at: options.temporal?.valid_at,
      known_at: options.temporal?.known_at,
    });
    const pool = await this.poolConnected();
    const pgStore = new PostgresTemporalStore(pool, this.options.schema);
    await pgStore.initialise();
    const events = await pgStore.list();
    const resolved = resolveAtStandpoint(events, subject, standpoint);
    const versions = await pgStore.versions();
    return {
      ok: true,
      schema: this.options.schema,
      subject,
      value: resolved.value,
      status: resolved.status,
      detail: resolved.reason,
      provenance: resolved.provenance,
      standpoint: { mode: standpoint.mode, valid_at: standpoint.validAt ?? null, known_at: standpoint.knownAt ?? null },
      trajectory: resolved.trajectory,
      trajectory_truncated: false,
      incomplete_history: resolved.incompleteHistory,
      reason: resolved.reason,
      route: { route: route.route, route_source: route.routeSource, route_reason: route.routeReason, route_ambiguous: route.routeAmbiguous },
      store_version: versions.storeVersion,
      event_schema_version: versions.eventSchemaVersion,
      reducer_version: TEMPORAL_REDUCER_VERSION,
    };
  }

  // -- Stage 3B: frame --------------------------------------------------

  async frameEstablish(work: { mode?: string; work_type?: string; objective?: string; prior_work_type?: string; signals?: Array<{ signal_id?: string; kind?: string; text?: string; observed_at?: string }> } = {}): Promise<Record<string, unknown>> {
    const { loadProjectFrame } = await import("./frame/service");
    const { frame, error } = await loadProjectFrame(this.options.projectDirectory);
    return { ...establishFrame(frame, error, work) };
  }

  // -- Stage 3C: trust --------------------------------------------------

  async trustDecisions(sourceIds: string[], requiredLevel: TrustLevel = "FULL"): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresStandingStore(pool, this.options.schema);
    await pgStore.initialise();
    const { policy, valid, error } = await loadTrustPolicy(this.options.projectDirectory);
    if (!valid) throw new EngineError("TRUST_POLICY_INVALID", error ?? "invalid trust policy.");
    const standing = resolveStanding(await pgStore.list());
    const { decideTrust } = await import("./trust/standing");
    return {
      decisions: sourceIds.map((id) => decideTrust(policy, standing, id, requiredLevel)),
      policyVersion: policy.version,
      policyDigest: policy.digest,
    };
  }

  // -- Stage 3E: trace --------------------------------------------------

  async traceGet(traceId: string): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    return (await service.get(traceId)) as unknown as Record<string, unknown>;
  }

  async traceFind(filters: { source_id?: string; chunk_id?: string; route?: string; work_type?: string; terminal_stage?: string; before?: string; after?: string; limit?: number } = {}): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    return { traces: await service.find({ sourceId: filters.source_id, chunkId: filters.chunk_id, route: filters.route, workType: filters.work_type, terminalStage: filters.terminal_stage, before: filters.before, after: filters.after, limit: filters.limit }) };
  }

  async traceExplain(traceId: string, candidateId: string): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    return await service.explain(traceId, candidateId);
  }

  async traceVerify(traceId: string): Promise<{ valid: boolean; reason: string }> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    return service.verify(traceId);
  }

  async traceReplay(traceId: string, replayKind: "trust" | "selection", persist = false): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    const { replayed, persisted } = await service.replay(traceId, replayKind, (c) => c, persist, "current");
    return { ...(replayed as unknown as Record<string, unknown>), persisted };
  }

  async traceDiff(traceId: string, diffWith: string): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const service = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    return service.diff(traceId, diffWith);
  }

  // -- Stage 3F: loops --------------------------------------------------

  async loopCreate(transition: Record<string, unknown>): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresLoopStore(pool, this.options.schema);
    await pgStore.initialise();
    const event = validateLoopEvent({ kind: "create", at: new Date().toISOString(), ...(transition as object) });
    if (!event.eventId || !event.loopId) throw new EngineError("LOOP_INVALID", "loop create requires event_id and loop_id.");
    const existing = (await pgStore.list()).filter((e) => e.loopId === event.loopId);
    if (existing.length > 0) {
      const views = reduceLoopEvents(await pgStore.list());
      return { duplicate: true, ...(views.get(event.loopId) as unknown as Record<string, unknown>) };
    }
    await pgStore.append(event);
    const views = reduceLoopEvents(await pgStore.list());
    return { duplicate: false, ...(views.get(event.loopId) as unknown as Record<string, unknown>) };
  }

  async loopAppend(event: Record<string, unknown>): Promise<{ duplicate: boolean }> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresLoopStore(pool, this.options.schema);
    await pgStore.initialise();
    return pgStore.append(validateLoopEvent(event));
  }

  async openLoops(request: { action?: string; loop_id?: string; state?: LoopState; subject?: string; transition_kind?: string; limit?: number } = {}): Promise<Record<string, unknown>> {
    const action = request.action ?? "list";
    const pool = await this.poolConnected();
    const pgStore = new PostgresLoopStore(pool, this.options.schema);
    await pgStore.initialise();
    const views = reduceLoopEvents(await pgStore.list());
    const all = [...views.values()].sort((a, b) => (a.createdAt < b.createdAt ? -1 : 1));
    if (action === "get" || action === "history") {
      if (!request.loop_id?.trim()) throw new EngineError("LOOP_INVALID", `loops action '${action}' requires loop_id.`);
      const view = views.get(request.loop_id);
      if (!view) throw new EngineError("LOOP_INVALID", `no loop ${JSON.stringify(request.loop_id)}.`);
      return { ...(view as unknown as Record<string, unknown>) };
    }
    let filtered = all;
    if (request.state) filtered = filtered.filter((v) => v.state === request.state);
    if (request.subject) filtered = filtered.filter((v) => v.subject.includes(request.subject as string));
    if (request.transition_kind) filtered = filtered.filter((v) => v.transitionKind === request.transition_kind);
    const limit = request.limit ?? 20;
    return { loops: filtered.slice(0, limit), total: filtered.length };
  }

  // -- Stage 3G: writes ---------------------------------------------------

  private async writePolicy(): Promise<import("./write/policy").WritePolicy> {
    const { policy, valid, error } = await loadWritePolicy(this.options.projectDirectory);
    if (!valid) throw new EngineError("WRITE_POLICY_INVALID", error ?? "invalid write policy.");
    return policy;
  }

  private async indexExplicitRecord(pool: Pool, record: { recordId: string; content: string; role: string }): Promise<{ chunks: number; embedded: number }> {
    const probe = await this.embedder.embed([record.content]);
    const store = this.store(pool);
    await store.initialise(probe.dimension);
    const sourceId = `memory://${record.recordId}`;
    await store.upsertSource(sourceId, "memory-record", sha1(record.content), null);
    const version = `${probe.provider}:${probe.model}:${probe.dimension}`;
    await store.replaceChunks(sourceId, [{
      chunkId: record.recordId.slice(0, 16).padEnd(16, "0"),
      sourceId,
      ordinal: 0,
      text: record.content,
      section: null,
      charStart: 0,
      charEnd: record.content.length,
      contentHash: sha1(record.content),
      chunker: "explicit:0.1.0",
      embeddingVersion: version,
      embedding: probe.vectors[0] ?? [],
    }]);
    return { chunks: 1, embedded: 1 };
  }

  async remember(input: WriteInput): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    const policy = await this.writePolicy();
    const service = new WriteService(pgStore, policy, () => new Date().toISOString(), async (record) => {
      await this.indexExplicitRecord(pool, record);
    });
    const out = await service.execute(input);
    if (!out.ok) return { ...out, schema: this.options.schema };
    const indexed = out.record ? { indexed: true, chunks: 1, embedded: 1 } : undefined;
    return { ...out, record_id: out.recordId, target_record_id: out.targetRecordId, action_id: out.actionId, index: indexed, schema: this.options.schema };
  }

  async recordShow(recordId: string): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    const record = await pgStore.getRecord(recordId);
    if (!record) throw new EngineError("MEMORY_TARGET_NOT_FOUND", `no memory record ${JSON.stringify(recordId)}.`);
    return { ...(record as unknown as Record<string, unknown>) };
  }

  async writeHistory(recordId: string): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    return { record_id: recordId, history: await pgStore.history(recordId) };
  }

  async writeRebuild(): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    const mem = new MemoryWriteStore();
    void mem;
    let reindexed = 0;
    // Re-index every active record through the native baseline store.
    const client = await pool.connect();
    try {
      const s = ident(this.options.schema);
      const res = await client.query(`SELECT record_id, content, role FROM ${s}.memory_records WHERE state = 'active'`);
      for (const row of res.rows) {
        await this.indexExplicitRecord(pool, {
          recordId: row.record_id as string,
          content: row.content as string,
          role: row.role as string,
        });
        reindexed += 1;
      }
    } finally {
      client.release();
    }
    return { ok: true, schema: this.options.schema, reindexed };
  }

  // -- Native context pipeline ---------------------------------------------

  async context(
    query: string,
    maxChars: number,
    maxResults: number,
    routeRequest: RouteRequest = "auto",
    temporal?: { mode?: string; valid_at?: string; known_at?: string },
    work?: { mode?: string; work_type?: string; objective?: string; prior_work_type?: string; signals?: Array<{ signal_id?: string; kind?: string; text?: string; observed_at?: string }> },
    trust?: { caller_scope?: string; level?: TrustLevel },
    selection?: { mode?: "decisive" | "full" },
  ): Promise<Record<string, unknown>> {
    const trimmed = query.trim();
    const route = resolveRoute(trimmed, routeRequest);
    const standpoint = validateStandpoint({
      mode: temporal?.mode ?? "current",
      valid_at: temporal?.valid_at,
      known_at: temporal?.known_at,
    });
    const search = await this.search(trimmed || "test", maxResults);
    const { loadProjectFrame } = await import("./frame/service");
    const { frame, error: frameError } = await loadProjectFrame(this.options.projectDirectory);
    const frameResult = establishFrame(frame, frameError, work ?? {});
    const pool = await this.poolConnected();
    const { policy: trustPolicy, valid: trustValid, error: trustError } = await loadTrustPolicy(this.options.projectDirectory);
    if (!trustValid) throw new EngineError("TRUST_POLICY_INVALID", trustError ?? "invalid trust policy.");
    const standingStore = new PostgresStandingStore(pool, this.options.schema);
    await standingStore.initialise();
    const standing = resolveStanding(await standingStore.list());
    const requiredLevel = trust?.level ?? "FULL";
    const candidates = (search.items ?? []).map((item) => ({
      candidateId: item.chunk_id,
      sourceId: item.source_id,
      text: item.text,
      section: item.section,
      score: item.score,
      provenance: `retrieval:${item.stage}`,
      lexicalRank: item.lexical_rank,
      denseRank: item.dense_rank,
    }));
    const staged = runPipelineStages(candidates, {
      query: trimmed,
      route,
      standpoint,
      frame: frameResult,
      trustPolicy,
      standing,
      requiredLevel,
      selectionMode: selection?.mode ?? "decisive",
      budget: { maxChars, maxResults },
    });
    const traceService = new TraceService(new PostgresTraceStore(pool, this.options.schema));
    await new PostgresTraceStore(pool, this.options.schema).initialise();
    const traceRecord = await traceService.create({
      query: trimmed,
      route: route.route,
      workType: frameResult.establishment?.workType ?? null,
      candidates: candidates.map((c) => {
        const stage = staged.stageMap[c.candidateId];
        return {
          candidateId: c.candidateId,
          sourceId: c.sourceId,
          text: c.text,
          score: c.score,
          stages: stage?.stages ?? { retrieved: { admitted: true, reason: "retrieved." } },
          terminalStage: stage?.terminalStage ?? "retrieved",
        };
      }),
      policies: {
        retrieval: `${this.options.retrieval.mode}:${this.options.retrieval.reranker}`,
        temporal: standpoint.mode,
        frame: frameResult.applied ? "framing-engine-v0.1" : "none",
        trust: trustPolicy.version,
        trustDigest: trustPolicy.digest,
        selection: selection?.mode ?? "decisive",
      },
      budget: { maxChars, maxResults },
    });
    const items = staged.selection.selected.map((s, i) => {
      const full = candidates.find((c) => c.candidateId === s.candidateId);
      return {
        chunk_id: s.candidateId,
        source_id: s.sourceId,
        section: full?.section ?? null,
        rank: i + 1,
        score: s.score,
        text: s.text,
        lexical_rank: full?.lexicalRank ?? null,
        dense_rank: full?.denseRank ?? null,
        stage: "selected",
      };
    });
    return {
      ok: true,
      indexed: search.indexed,
      schema: this.options.schema,
      items,
      trace: search.trace,
      trace_id: traceRecord.traceId,
      content: staged.content,
      chars: staged.chars,
      route: { route: route.route, route_source: route.routeSource, route_reason: route.routeReason, route_ambiguous: route.routeAmbiguous },
      temporal: { mode: standpoint.mode, valid_at: standpoint.validAt ?? null, known_at: standpoint.knownAt ?? null },
      frame: {
        applied: frameResult.applied,
        reason: frameResult.reason,
        control: frameResult.control,
        control_reason: frameResult.controlReason,
        establishment: frameResult.establishment ? {
          establishment: frameResult.establishment.establishment,
          work_type: frameResult.establishment.workType,
          objective: frameResult.establishment.objective,
          supporting_refs: frameResult.establishment.supportingRefs,
          conflicting_refs: frameResult.establishment.conflictingRefs,
          reasons: frameResult.establishment.reasons,
          source: frameResult.establishment.source,
          prior_work_type: frameResult.establishment.priorWorkType,
        } : undefined,
        project_frame_version: frameResult.projectFrameVersion,
        project_frame_digest: frameResult.projectFrameDigest,
      },
      trust: {
        mode: route.route,
        level: requiredLevel,
        policy_version: trustPolicy.version,
        policy_source: trustPolicy.source,
        admitted: staged.trustSummary.admitted,
        denied: staged.trustSummary.denied,
        quarantined: staged.trustSummary.quarantined,
      },
      selection: {
        policy_version: SELECT_POLICY_VERSION,
        input_count: staged.selection.summary.inputCount,
        selected_count: staged.selection.summary.selectedCount,
        dropped_redundant: staged.selection.summary.droppedRedundant,
        dropped_low_value: staged.selection.summary.droppedLowValue,
        dropped_budget: staged.selection.summary.droppedBudget,
        chars_before: staged.selection.summary.charsBefore,
        chars_after: staged.selection.summary.charsAfter,
        compression_ratio: staged.selection.summary.compressionRatio,
        budget_insufficient: staged.selection.summary.budgetInsufficient,
      },
      trace_persisted: true,
      admission_note: staged.admissionNote,
    };
  }

  // -- Native health across all subsystems -----------------------------------

  async nativeDoctor(): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const baseline = await doctorBaseline(pool, {
      dsn: this.options.dsn,
      schema: this.options.schema,
      projectDirectory: this.options.projectDirectory,
      embedding: this.options.embedding,
    });
    const temporalStore = new PostgresTemporalStore(pool, this.options.schema);
    await temporalStore.initialise();
    const temporalService = new TemporalService(temporalStore);
    const temporalHealth = await temporalService.health();
    const frame = await frameHealth(this.options.projectDirectory);
    const { policy: trustPolicy, valid: trustValid, error: trustError } = await loadTrustPolicy(this.options.projectDirectory);
    const standingStore = new PostgresStandingStore(pool, this.options.schema);
    await standingStore.initialise();
    const standingEvents = await standingStore.list();
    const standing = resolveStanding(standingEvents);
    const traceStore = new PostgresTraceStore(pool, this.options.schema);
    await traceStore.initialise();
    const traceCounts = await traceStore.count();
    const loopStore = new PostgresLoopStore(pool, this.options.schema);
    await loopStore.initialise();
    const loopEvents = await loopStore.list();
    let loopViews: Array<{ state: string; evidenceRefs: string[] }> = [];
    let loopError: string | undefined;
    try {
      loopViews = [...reduceLoopEvents(loopEvents).values()];
    } catch (error) {
      loopError = error instanceof Error ? error.message.slice(0, 160) : String(error).slice(0, 160);
    }
    const writeStore = new PostgresWriteStore(pool, this.options.schema);
    await writeStore.initialise();
    const writeCounts = await writeStore.counts();
    const { policy: writePolicy, valid: writeValid, error: writePolicyError } = await loadWritePolicy(this.options.projectDirectory);
    const countBy = (state: string) => loopViews.filter((v) => v.state === state).length;
    const result: Record<string, unknown> = {
      ...baseline,
      native: true,
      engine_root: "typescript:src/engine",
      python_dependencies: {},
      temporal: {
        store_ready: true,
        store_version: TEMPORAL_STORE_VERSION,
        event_schema_version: TEMPORAL_EVENT_SCHEMA,
        reducer_version: TEMPORAL_REDUCER_VERSION,
        events: temporalHealth.events,
        subjects: temporalHealth.subjects,
        unknown_references: temporalHealth.unknownReferences,
        causality_violations: temporalHealth.causalityViolations,
        sequence_gaps: temporalHealth.sequenceGaps,
        ...(temporalHealth.error ? { error: temporalHealth.error } : {}),
      },
      frame: {
        project_frame_present: frame.projectFramePresent,
        project_frame_valid: frame.projectFrameValid,
        project_frame_version: frame.projectFrameVersion,
        project_frame_digest: frame.projectFrameDigest,
        known_work_types: frame.knownWorkTypes,
        framing_engine_version: FRAMING_ENGINE_VERSION,
        frame_error: frame.frameError,
      },
      trust: {
        trust_engine_version: TRUST_ENGINE_VERSION,
        configured: trustPolicy.configured,
        policy_source: trustPolicy.source,
        policy_version: trustPolicy.version,
        policy_digest: trustPolicy.digest,
        policy_valid: trustValid,
        instruction_screen_version: INSTRUCTION_SCREEN_VERSION,
        standing_store_ready: true,
        standing_events: standingEvents.length,
        revoked_sources: [...standing.revoked].sort(),
        restricted_sources: Object.fromEntries([...standing.restricted.entries()].map(([k, v]) => [k, [v]])),
        policy_error: trustError,
      },
      selection: {
        select_engine_version: "select-engine-v0.1",
        policy_version: SELECT_POLICY_VERSION,
        budget_version: BUDGET_VERSION,
        redundancy_version: REDUNDANCY_VERSION,
        provenance_version: PROVENANCE_VERSION,
      },
      trace: {
        trace_engine_version: TRACE_ENGINE_VERSION,
        store_version: TRACE_STORE_VERSION,
        schema_version: TRACE_SCHEMA_VERSION,
        replay_version: REPLAY_PROTOCOL_VERSION,
        ready: true,
        trace_count: traceCounts.traces,
        oldest_trace: traceCounts.oldest,
        newest_trace: traceCounts.newest,
        retention: "indefinite",
      },
      loops: {
        loops_engine_version: LOOP_ENGINE_VERSION,
        store_version: LOOP_STORE_VERSION,
        event_schema_version: LOOP_EVENT_SCHEMA,
        reducer_version: LOOP_REDUCER_VERSION,
        closure_version: LOOP_CLOSURE_VERSION,
        ready: true,
        event_count: loopEvents.length,
        loop_count: loopViews.length,
        open: countBy("open"),
        completed: countBy("completed"),
        cancelled: countBy("cancelled"),
        superseded: countBy("superseded"),
        uncertain: countBy("uncertain"),
        unresolved_evidence_refs: [],
        ...(loopError ? { error: loopError } : {}),
      },
      writes: {
        write_engine_version: WRITE_ENGINE_VERSION,
        store_version: WRITE_STORE_VERSION,
        record_schema_version: RECORD_SCHEMA_VERSION,
        action_schema_version: ACTION_SCHEMA_VERSION,
        relation_version: RELATION_VERSION,
        ready: true,
        policy: {
          configured: writePolicy.configured,
          valid: writeValid,
          version: writePolicy.version,
          digest: writePolicy.digest,
          source: writePolicy.source,
          error: writePolicyError,
        },
        record_count: writeCounts.records,
        action_count: writeCounts.actions,
        remember_count: writeCounts.byAction["remember"] ?? 0,
        correct_count: writeCounts.byAction["correct"] ?? 0,
        supersede_count: writeCounts.byAction["supersede"] ?? 0,
        retract_count: writeCounts.byAction["retract"] ?? 0,
        relationship_count: (writeCounts.byAction["correct"] ?? 0) + (writeCounts.byAction["supersede"] ?? 0) + (writeCounts.byAction["retract"] ?? 0),
        unresolved_index_records: [],
        last_action_at: writeCounts.lastActionAt,
      },
    };
    // Whole-product readiness: baseline gates everything, then policies,
    // then subsystem stores. No memory semantics change here.
    const { computeProductReadiness } = await import("./readiness");
    const baselineReadiness = (baseline["readiness"] as { code: string; reasons: string[] } | undefined) ??
      (baseline["ok"] === true
        ? { code: "HEALTHY", reasons: [] }
        : { code: "SUBSYSTEM_FAILURE", reasons: ["baseline not ready"] });
    const product = computeProductReadiness(
      {
        ok: baseline["ok"] === true,
        code: baselineReadiness.code as import("./readiness").ReadinessCode,
        reasons: baselineReadiness.reasons,
      },
      {
        temporalReady: (temporalHealth as { storeReady?: boolean }).storeReady === true,
        standingReady: true,
        trustPolicyValid: trustValid,
        trustPolicyError: trustError,
        traceReady: true,
        loopsReady: loopError === undefined,
        loopsError: loopError,
        writesReady: true,
        writePolicyValid: writeValid,
        writePolicyError: writePolicyError,
        frameError: frame.frameError,
      },
    );
    result["ok"] = product.ok;
    result["readiness"] = { code: product.code, reasons: product.reasons };
    if (product.ok) {
      result["message"] = baseline["message"] ?? "native Remembering engine is ready.";
    } else if (product.code !== ((baseline["readiness"] as { code?: string } | undefined)?.code ?? "")) {
      result["message"] = `${product.code}: ${product.reasons.join("; ")}`;
    }
    return result;
  }

  // -- File imports (trust / loops / explicit memory) --------------------------

  private async importJsonLines(
    relativePath: string,
    handle: (raw: unknown, lineNumber: number) => Promise<{ duplicate: boolean }>,
  ): Promise<{ imported: number; duplicates: number; failed: string[] }> {
    const { readFile, realpath } = await import("node:fs/promises");
    const { join, resolve } = await import("node:path");
    let root: string;
    try {
      root = await realpath(this.options.projectDirectory);
    } catch {
      root = resolve(this.options.projectDirectory);
    }
    const file = join(root, relativePath);
    let content: string;
    try {
      content = await readFile(file, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { imported: 0, duplicates: 0, failed: [] };
      }
      throw error;
    }
    let imported = 0;
    let duplicates = 0;
    const failed: string[] = [];
    const lines = content.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i] as string;
      if (!line.trim()) continue;
      let raw: unknown;
      try {
        raw = JSON.parse(line);
      } catch {
        failed.push(`line ${i + 1}: not JSON.`);
        continue;
      }
      try {
        const out = await handle(raw, i + 1);
        if (out.duplicate) duplicates += 1;
        else imported += 1;
      } catch (error) {
        const code = (error as { code?: string })?.code ?? "IMPORT_INVALID";
        failed.push(`line ${i + 1}: ${code}.`);
      }
    }
    return { imported, duplicates, failed };
  }

  async trustImport(): Promise<{ ok: boolean; schema: string; message: string; imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] }> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresStandingStore(pool, this.options.schema);
    await pgStore.initialise();
    const outcome = await this.importJsonLines(joinTrustEvents(), async (raw) => pgStore.append(validateStandingEvent(raw)));
    const events = await pgStore.count();
    return {
      ok: outcome.failed.length === 0,
      schema: this.options.schema,
      message: outcome.failed.length ? `standing import failures: ${outcome.failed.join("; ")}` : `standing events +${outcome.imported} (${events} total).`,
      ...outcome,
      events,
      subjects: [],
    };
  }

  async loopImport(): Promise<{ ok: boolean; schema: string; message: string; imported: number; duplicates: number; failed: string[]; events: number; subjects: string[] }> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresLoopStore(pool, this.options.schema);
    await pgStore.initialise();
    const outcome = await this.importJsonLines(joinLoopEvents(), async (raw) => pgStore.append(validateLoopEvent(raw)));
    const views = reduceLoopEvents(await pgStore.list());
    return {
      ok: outcome.failed.length === 0,
      schema: this.options.schema,
      message: outcome.failed.length ? `loop import failures: ${outcome.failed.join("; ")}` : `loop events +${outcome.imported} (${views.size} loops).`,
      ...outcome,
      events: await pgStore.count(),
      subjects: [...views.values()].map((v) => v.subject),
    };
  }

  async memoryImport(): Promise<{ imported: number; duplicates: number; failed: string[]; denied: string[]; events: number }> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    const policy = await this.writePolicy();
    const service = new WriteService(pgStore, policy, () => new Date().toISOString(), async (record) => {
      await this.indexExplicitRecord(pool, record);
    });
    let imported = 0;
    let duplicates = 0;
    const failed: string[] = [];
    const denied: string[] = [];
    const outcome = await this.importJsonLines(joinMemoryEvents(), async (raw) => {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new EngineError("IMPORT_INVALID", "memory event must be an object.");
      const r = raw as Record<string, unknown>;
      const out = await service.execute({
        action: (r["action"] as WriteInput["action"] | undefined) ?? "remember",
        content: r["content"] as string | undefined,
        targetRecordId: (r["target_record_id"] as string | undefined) ?? undefined,
        role: (r["role"] as WriteInput["role"] | undefined) ?? "ordinary",
        reason: (r["reason"] as string | undefined) ?? undefined,
        evidenceRefs: Array.isArray(r["evidence_refs"]) ? (r["evidence_refs"] as string[]) : [],
        callerScope: (r["caller_scope"] as string | undefined) ?? "import",
        origin: "cli",
        eventTime: r["event_time"] as string | undefined,
        effectiveFrom: r["effective_from"] as string | undefined,
        idempotencyKey: (r["idempotency_key"] as string | undefined) ?? (r["event_id"] as string | undefined),
      });
      if (!out.ok) {
        if (out.code === "MEMORY_WRITE_DENIED") denied.push(out.reason ?? "denied");
        else throw new EngineError(out.code ?? "IMPORT_INVALID", out.reason ?? "import failed.");
      }
      return { duplicate: out.duplicate ?? false };
    });
    imported = outcome.imported;
    duplicates = outcome.duplicates;
    failed.push(...outcome.failed);
    const counts = await pgStore.counts();
    return { imported, duplicates, failed, denied, events: counts.actions };
  }

  async loopRebuild(): Promise<Record<string, unknown>> {
    const pool = await this.poolConnected();
    const pgStore = new PostgresLoopStore(pool, this.options.schema);
    await pgStore.initialise();
    const events = await pgStore.list();
    const views = reduceLoopEvents(events);
    return { ok: true, schema: this.options.schema, events: events.length, loops: views.size };
  }

  async actionShow(actionId: string): Promise<Record<string, unknown>> {
    if (!actionId.trim()) throw new EngineError("CONFIG_INVALID", "action_show requires action_id.");
    const pool = await this.poolConnected();
    const pgStore = new PostgresWriteStore(pool, this.options.schema);
    await pgStore.initialise();
    const action = await pgStore.getAction(actionId.trim());
    if (!action) return { ok: false, schema: this.options.schema, message: "WRITE_ACTION_NOT_FOUND" };
    const record = action.recordId ? await pgStore.getRecord(action.recordId) : null;
    return { ok: true, schema: this.options.schema, action, record };
  }

  async captureSession(
    sessionId: string,
    facts: { agent?: string; provider?: string; model?: string; messages: Array<{ id?: string; role: string; text: string; agent?: string; model?: string; tool_calls?: unknown[]; tool_results?: unknown[]; observed_at?: string }> },
  ): Promise<Record<string, unknown>> {
    const { captureSession } = await import("./sessions");
    const result = await captureSession(this.options.projectDirectory, this.options.schema, sessionId, {
      agent: facts.agent,
      provider: facts.provider,
      model: facts.model,
      messages: facts.messages.map((m) => ({
        id: m.id, role: m.role, text: m.text, agent: m.agent, model: m.model,
        toolCalls: m.tool_calls as unknown[] | undefined, toolResults: m.tool_results as unknown[] | undefined,
        observedAt: m.observed_at,
      })),
    });
    return {
      ok: true,
      schema: this.options.schema,
      session_id: result.sessionId,
      transcript_path: result.transcriptPath,
      recorded: result.recorded,
      duplicates: result.duplicates,
      total: result.total,
    };
  }

  // -- Native evaluations ---------------------------------------------------------

  async routeEval(): Promise<Record<string, unknown>> {
    const { routeEval } = await import("./evaluations");
    return (await routeEval()) as unknown as Record<string, unknown>;
  }

  async temporalEval(): Promise<Record<string, unknown>> {
    const { temporalEval } = await import("./evaluations");
    return (await temporalEval()) as unknown as Record<string, unknown>;
  }

  async frameEval(): Promise<Record<string, unknown>> {
    const { frameEval } = await import("./evaluations");
    return (await frameEval()) as unknown as Record<string, unknown>;
  }

  async trustEval(): Promise<Record<string, unknown>> {
    const { trustEval } = await import("./evaluations");
    return (await trustEval()) as unknown as Record<string, unknown>;
  }

  async selectionEval(): Promise<Record<string, unknown>> {
    const { selectionEval } = await import("./evaluations");
    return (await selectionEval()) as unknown as Record<string, unknown>;
  }

  async traceEval(): Promise<Record<string, unknown>> {
    const { traceEval } = await import("./evaluations");
    return (await traceEval()) as unknown as Record<string, unknown>;
  }

  async loopEval(): Promise<Record<string, unknown>> {
    const { loopEval } = await import("./evaluations");
    return (await loopEval()) as unknown as Record<string, unknown>;
  }

  async writeEval(): Promise<Record<string, unknown>> {
    const { writeEval } = await import("./evaluations");
    return (await writeEval()) as unknown as Record<string, unknown>;
  }
}

function joinTrustEvents(): string {
  return [".remembering", "trust", "events.jsonl"].join("/");
}

function joinLoopEvents(): string {
  return [".remembering", "loops", "events.jsonl"].join("/");
}

function joinMemoryEvents(): string {
  return [".remembering", "memory", "events.jsonl"].join("/");
}
