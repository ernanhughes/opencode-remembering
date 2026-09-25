import type { RememberingConfig } from "./config";
import { RememberingEngine } from "./engine/pipeline";

export type HealthReport = {
  ok: boolean;
  engine_root: string;
  engine_version: string;
  engine_available: boolean;
  python_dependencies: Record<string, boolean>;
  dsn_redacted: string;
  schema: string;
  project_directory: string;
  postgres_reachable: boolean;
  database_exists: boolean;
  pgvector_available: boolean;
  pgvector_version: string | null;
  pg_trgm_available: boolean;
  schema_initialized: boolean;
  schema_identity_ok: boolean;
  embedding_provider_reachable: boolean;
  embedding_model_available: boolean;
  embedding_detail: string;
  stored_embedding_version: string | null;
  stored_embedding_dimension: number | null;
  configured_embedding_version: string;
  embedding_dimension_compatible: boolean | null;
  fts_index_present: boolean;
  hnsw_index_present: boolean;
  source_count: number | null;
  chunk_count: number | null;
  temporal: {
    store_ready: boolean;
    store_version: string | null;
    event_schema_version: string | null;
    reducer_version: string | null;
    events: number;
    subjects: string[];
    unknown_references: string[];
    causality_violations: string[];
    sequence_gaps: Array<Record<string, unknown>>;
    error?: string;
  };
  frame: {
    project_frame_present: boolean;
    project_frame_valid: boolean;
    project_frame_version: string | null;
    project_frame_digest: string | null;
    known_work_types: string[];
    framing_engine_version: string;
    frame_error: string | null;
  };
  trust: {
    trust_engine_version: string;
    configured: boolean;
    policy_source: string;
    policy_version: string | null;
    policy_digest: string | null;
    policy_valid: boolean;
    instruction_screen_version: string;
    standing_store_ready: boolean;
    standing_events: number;
    revoked_sources: string[];
    restricted_sources: Record<string, string[]>;
    policy_error: string | null;
  };
  trace: {
    trace_engine_version: string;
    store_version: string | null;
    schema_version: string;
    replay_version: string;
    ready: boolean;
    trace_count: number;
    oldest_trace: string | null;
    newest_trace: string | null;
    retention: string;
    error?: string;
  };
  loops: {
    loops_engine_version: string;
    store_version: string | null;
    event_schema_version: string;
    reducer_version: string;
    closure_version: string;
    ready: boolean;
    event_count: number;
    loop_count: number;
    open: number;
    completed: number;
    cancelled: number;
    superseded: number;
    uncertain: number;
    unresolved_evidence_refs: string[];
    error?: string;
  };
  writes: {
    write_engine_version: string;
    store_version: string | null;
    record_schema_version: string;
    action_schema_version: string;
    relation_version: string;
    ready: boolean;
    policy: {
      configured: boolean;
      valid: boolean;
      version: string | null;
      digest: string | null;
      source: string;
      error: string | null;
    };
    record_count: number;
    action_count: number;
    remember_count: number;
    correct_count: number;
    supersede_count: number;
    retract_count: number;
    relationship_count: number;
    unresolved_index_records: string[];
    last_action_at: string | null;
    error?: string;
  };
  selection: {
    select_engine_version: string;
    policy_version: string;
    budget_version: string;
    redundancy_version: string;
    provenance_version: string;
  };
  // Backward-compatible bootstrap fields.
  indexed: boolean;
  chunks: number | null;
  message?: string;
};

export type MemoryRoute = "recall" | "influence";
export type RouteRequest = "auto" | MemoryRoute;

export type RouteBlock = {
  route: MemoryRoute;
  route_source: "explicit" | "deterministic";
  route_reason: string;
  route_ambiguous: boolean;
};

export type RetrievalTraceSummary = {
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
  embedding: {
    provider: string;
    model: string;
    dimension: number;
    version: string;
  };
  retrieval: {
    mode: string;
    lexical_k: number;
    dense_k: number;
    fusion_k: number;
    rerank_k: number;
    reranker: string;
  };
  dense_executed: boolean;
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
  trace: RetrievalTraceSummary | null;
  message?: string;
};

export type ContextResult = SearchResult & {
  trace_id: string;
  content: string;
  chars: number;
  route: RouteBlock;
  temporal: TemporalEcho;
  frame: FrameBlock;
  trust: TrustSummary;
  selection: SelectionSummary;
  trace_persisted: boolean;
  admission_note: string;
};

export type StateResult = {
  ok: boolean;
  schema: string;
  subject: string;
  value: string | null;
  status: string;
  detail: string;
  provenance: string[];
  standpoint: TemporalEcho;
  trajectory: Array<Record<string, unknown>>;
  trajectory_truncated: boolean;
  incomplete_history: boolean;
  reason: string;
  route: RouteBlock;
  store_version: string | null;
  event_schema_version: string | null;
  reducer_version: string | null;
};

export type TemporalImportResult = {
  ok: boolean;
  schema: string;
  message: string;
  imported: number;
  duplicates: number;
  failed: string[];
  events: number;
  subjects: string[];
};

export type WorkSignalRequest = {
  signal_id: string;
  kind: string;
  text: string;
  observed_at: string;
};

export type WorkRequest = {
  mode?: "auto" | "explicit" | "none";
  work_type?: string;
  objective?: string;
  prior_work_type?: string;
  signals?: WorkSignalRequest[];
};

export type FrameBlock = {
  applied: boolean;
  reason: string;
  control?: "hard" | "soft" | "query_only";
  control_reason?: string;
  establishment?: {
    establishment: string;
    work_type: string | null;
    objective: string;
    supporting_refs: string[];
    conflicting_refs: string[];
    reasons: string[];
    source: string;
    prior_work_type: string | null;
  };
  project_frame_version?: string | null;
  project_frame_digest?: string | null;
  excluded?: Array<Record<string, unknown>>;
};

export type SelectionRequest = {
  mode?: "decisive" | "full";
};

export type LoopState = "open" | "completed" | "cancelled" | "superseded" | "uncertain";

export type LoopAction = "list" | "get" | "history";

export type LoopRequest = {
  action?: LoopAction;
  loop_id?: string;
  state?: LoopState;
  subject?: string;
  transition_kind?: string;
  limit?: number;
};

export type LoopView = {
  loop_id: string;
  subject: string;
  transition_kind: string;
  state: LoopState;
  reason: string;
  expected: Record<string, unknown>;
  evidence_refs: string[];
  closure: Record<string, unknown>;
  history: Array<Record<string, unknown>>;
  created_at: string;
  resolved_at: string;
  search_complete: boolean;
};

export type LoopEvaluation = {
  ok: boolean;
  categories: Record<
    string,
    { correct: number; total: number; failures: string[] }
  >;
  checks_total: number;
  checks_passed: number;
  passed: boolean;
  eval_version: string;
};

export type TemporalStandpointRequest = {
  mode?: "current" | "valid_at" | "as_known" | "bitemporal";
  valid_at?: string;
  known_at?: string;
};

export type TrustRequest = {
  caller_scope?: string;
  level?: "T0" | "S1" | "S2" | "S3" | "FULL";
};

export type TraceRequest =
  | { mode: "get"; trace_id: string }
  | { mode: "verify"; trace_id: string }
  | {
      mode: "find";
      filters?: {
        source_id?: string;
        chunk_id?: string;
        route?: string;
        work_type?: string;
        policy_stage?: string;
        policy_version?: string;
        terminal_stage?: string;
        before?: string;
        after?: string;
        limit?: number;
      };
    }
  | { mode: "explain"; trace_id: string; candidate_id: string }
  | {
      mode: "replay";
      trace_id: string;
      replay_kind: "trust" | "selection";
      policy?: Record<string, unknown> | "current";
      selection_mode?: "decisive" | "full";
      persist?: boolean;
    }
  | { mode: "diff"; trace_id: string; diff_with: string };

export type SelectionSummary = {
  policy_version: string;
  input_count: number;
  selected_count: number;
  dropped_redundant: number;
  dropped_low_value: number;
  dropped_budget: number;
  chars_before: number;
  chars_after: number;
  compression_ratio: number;
  budget_insufficient: boolean;
};

export type TrustSummary = {
  mode: string;
  level: string;
  policy_version: string | null;
  policy_source: string | null;
  admitted: number;
  denied: number;
  quarantined: number;
};

export type TrustEvaluation = {
  ok: boolean;
  contract: {
    categories: Record<
      string,
      { correct: number; total: number; failures: string[] }
    >;
    checks_total: number;
    checks_passed: number;
    passed: boolean;
    eval_version: string;
  };
  ladder: {
    levels: Record<string, unknown>;
    eval_version: string;
  };
};

export type TemporalEcho = {
  mode: string;
  valid_at: string | null;
  known_at: string | null;
};

export type TemporalEvaluation = {
  ok: boolean;
  categories: Record<
    string,
    { correct: number; total: number; failures: string[] }
  >;
  checks_total: number;
  checks_passed: number;
  passed: boolean;
};

export type RouteEvalResult = {
  ok: boolean;
  examples: number;
  recall_correct: number;
  recall_total: number;
  influence_correct: number;
  influence_total: number;
  ambiguous_correct: number;
  ambiguous_total: number;
  override_correct: number;
  override_total: number;
  checks_total: number;
  checks_passed: number;
  failures: Array<Record<string, unknown>>;
  passed: boolean;
};

export type SetupStep = {
  name: string;
  ok: boolean;
  ms: number;
  detail: string;
};

export type SetupResult = {
  ok: boolean;
  schema: string;
  project_directory: string;
  steps: SetupStep[];
  embedding: {
    provider: string;
    model: string;
    dimension: number;
    version: string;
  };
  refresh: RefreshResult | null;
  message?: string;
};

export type TemporalImportSummary = {
  imported: number;
  duplicates: number;
  failed: string[];
  events: number;
  subjects: string[];
};

export type RefreshResult = {
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
  temporal?: TemporalImportSummary;
  message?: string;
};

export type CaptureMessage = {
  id?: string;
  role: string;
  text: string;
  agent?: string;
  model?: string;
  tool_calls?: unknown[];
  tool_results?: unknown[];
  observed_at?: string;
};

export type CaptureResult = {
  ok: boolean;
  schema: string;
  session_id: string;
  transcript_path: string;
  recorded: number;
  duplicates: number;
  total: number;
};

const TIMEOUTS_MS: Record<string, number> = {
  doctor: 30_000,
  search: 120_000,
  context: 120_000,
  capture_session: 60_000,
  setup: 1_200_000,
  refresh: 1_200_000,
  temporal_import: 120_000,
  state: 60_000,
  route_eval: 60_000,
  temporal_eval: 60_000,
  remember: 300_000,
  write_eval: 120_000,
  write_rebuild: 1_200_000,
};

function validateTrustRequest(trust: TrustRequest): void {
  if (trust.caller_scope !== undefined && !trust.caller_scope.trim()) {
    throw new Error("trust.caller_scope must be a non-empty string.");
  }
  const level = trust.level ?? "FULL";
  if (level !== "T0" && level !== "S1" && level !== "S2" && level !== "S3" && level !== "FULL") {
    throw new Error(
      `Invalid trust level ${JSON.stringify(trust.level)}: expected 'T0', 'S1', 'S2', 'S3' or 'FULL'.`,
    );
  }
}

function validateSelectionRequest(selection: SelectionRequest): void {
  const mode = selection.mode ?? "decisive";
  if (mode !== "decisive" && mode !== "full") {
    throw new Error(
      `Invalid selection mode ${JSON.stringify(selection.mode)}: expected 'decisive' or 'full'.`,
    );
  }
}

export type RememberAction = "remember" | "correct" | "supersede" | "retract";

export type RememberRole =
  | "ordinary"
  | "evidence"
  | "proposal"
  | "preference"
  | "decision"
  | "production_state";

export type RememberRequest = {
  action?: RememberAction;
  content?: string;
  target_record_id?: string;
  role?: RememberRole;
  reason?: string;
  evidence_refs?: string[];
  effective_from?: string;
  event_time?: string;
  idempotency_key?: string;
  caller_scope?: string;
  origin?: "opencode" | "cli";
};

export type RememberResult = {
  ok: boolean;
  code?: string;
  reason?: string;
  action_id?: string;
  action?: string;
  record_id?: string | null;
  target_record_id?: string | null;
  duplicate?: boolean;
  authorization?: {
    verdict: string;
    reason: string;
    policy_version: string;
    policy_digest: string;
    matched_grant_id: string | null;
  };
  record?: {
    role: string;
    standing_ceiling: string;
    source_id: string;
    lineage_root: string;
  } | null;
  index?: { indexed: boolean; chunks: number; embedded: number };
  relation?: { type: string; target_record_id: string } | null;
  latencies_ms?: Record<string, number>;
  schema?: string;
  message?: string;
};

export type WriteEvaluation = {
  ok: boolean;
  contract_categories: number;
  eval_version: string;
  checks_total: number;
  checks_passed: number;
  categories: Record<
    string,
    { correct: number; total: number; failures: string[] }
  >;
  passed: boolean;
};

const REMEMBER_ACTIONS: RememberAction[] = [
  "remember",
  "correct",
  "supersede",
  "retract",
];

const REMEMBER_ROLES: RememberRole[] = [
  "ordinary",
  "evidence",
  "proposal",
  "preference",
  "decision",
  "production_state",
];

function validateRememberRequest(request: RememberRequest): void {
  const action = request.action ?? "remember";
  if (!REMEMBER_ACTIONS.includes(action)) {
    throw new Error(
      `Invalid remember action ${JSON.stringify(request.action)}: expected 'remember', 'correct', 'supersede' or 'retract'.`,
    );
  }
  if (request.role !== undefined && !REMEMBER_ROLES.includes(request.role)) {
    throw new Error(
      `Invalid remember role ${JSON.stringify(request.role)}.`,
    );
  }
  if (
    request.caller_scope !== undefined &&
    !request.caller_scope.trim()
  ) {
    throw new Error("remember caller_scope must be a non-empty string.");
  }
  if (
    request.evidence_refs !== undefined &&
    !Array.isArray(request.evidence_refs)
  ) {
    throw new Error("remember evidence_refs must be a list.");
  }
}

const LOOP_ACTIONS: LoopAction[] = ["list", "get", "history"];
const LOOP_STATES: LoopState[] = ["open", "completed", "cancelled", "superseded", "uncertain"];

function validateLoopRequest(request: LoopRequest): void {
  const action = request.action ?? "list";
  if (!LOOP_ACTIONS.includes(action)) {
    throw new Error(
      `Invalid loops action ${JSON.stringify(request.action)}: expected 'list', 'get' or 'history'.`,
    );
  }
  if ((action === "get" || action === "history") && !request.loop_id?.trim()) {
    throw new Error(`Loops action '${action}' requires loop_id.`);
  }
  if (request.state !== undefined && !LOOP_STATES.includes(request.state)) {
    throw new Error(
      `Invalid loop state ${JSON.stringify(request.state)}.`,
    );
  }
  if (
    request.limit !== undefined &&
    (!Number.isInteger(request.limit) || request.limit < 1)
  ) {
    throw new Error("Loops limit must be a positive integer.");
  }
}

function validateTraceRequest(request: TraceRequest): void {
  if (request.mode === "get" || request.mode === "verify") {
    if (!request.trace_id.trim()) {
      throw new Error(`Trace mode '${request.mode}' requires trace_id.`);
    }
  } else if (request.mode === "find") {
    const filters = request.filters ?? {};
    if (
      filters.limit !== undefined &&
      (!Number.isInteger(filters.limit) || filters.limit < 1)
    ) {
      throw new Error("Trace find limit must be a positive integer.");
    }
  } else if (request.mode === "explain") {
    if (!request.trace_id.trim() || !request.candidate_id.trim()) {
      throw new Error(
        "Trace mode 'explain' requires trace_id and candidate_id.",
      );
    }
  } else if (request.mode === "replay") {
    if (!request.trace_id.trim()) {
      throw new Error("Trace mode 'replay' requires trace_id.");
    }
    if (request.replay_kind !== "trust" && request.replay_kind !== "selection") {
      throw new Error(
        `Invalid replay_kind ${JSON.stringify(request.replay_kind)}.`,
      );
    }
  } else if (request.mode === "diff") {
    if (!request.trace_id.trim() || !request.diff_with.trim()) {
      throw new Error(
        "Trace mode 'diff' requires trace_id and diff_with.",
      );
    }
  }
}

function validateWorkRequest(work: WorkRequest): void {
  const mode = work.mode ?? "auto";
  if (mode !== "auto" && mode !== "explicit" && mode !== "none") {
    throw new Error(
      `Invalid work mode ${JSON.stringify(work.mode)}: expected 'auto', 'explicit' or 'none'.`,
    );
  }
  if (mode === "explicit" && !work.work_type?.trim()) {
    throw new Error("Explicit work requires work_type.");
  }
  if (work.signals !== undefined && !Array.isArray(work.signals)) {
    throw new Error("work.signals must be a list.");
  }
}

function validateTemporalRequest(temporal: TemporalStandpointRequest): void {
  const mode = temporal.mode ?? "current";
  if (mode !== "current" && mode !== "valid_at" && mode !== "as_known" && mode !== "bitemporal") {
    throw new Error(
      `Invalid temporal mode ${JSON.stringify(temporal.mode)}: expected 'current', 'valid_at', 'as_known' or 'bitemporal'.`,
    );
  }
  if ((mode === "valid_at" || mode === "bitemporal") && !temporal.valid_at?.trim()) {
    throw new Error(`Temporal mode '${mode}' requires valid_at.`);
  }
  if ((mode === "as_known" || mode === "bitemporal") && !temporal.known_at?.trim()) {
    throw new Error(`Temporal mode '${mode}' requires known_at.`);
  }
}

function resolveEmbeddingAuth(config: RememberingConfig): { headers?: Record<string, string>; bearerToken?: string } {
  const out: { headers?: Record<string, string>; bearerToken?: string } = {};
  const headersEnv = (config.embedding as { headersEnv?: string }).headersEnv;
  if (headersEnv) {
    const raw = process.env[headersEnv];
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as Record<string, string>;
        if (parsed && typeof parsed === "object") out.headers = parsed;
      } catch { /* fail closed at request time, not here */ }
    }
  }
  const tokenEnv = (config.embedding as { authTokenEnv?: string }).authTokenEnv;
  if (tokenEnv && process.env[tokenEnv]) out.bearerToken = process.env[tokenEnv];
  else if (process.env.REMEMBERING_EMBEDDING_AUTH_TOKEN) out.bearerToken = process.env.REMEMBERING_EMBEDDING_AUTH_TOKEN;
  return out;
}

export class ProjectMemoryClient {
  private readonly engine: RememberingEngine;

  constructor(
    private readonly config: RememberingConfig,
    private readonly projectDirectory: string,
    engineOverride?: RememberingEngine,
  ) {
    const embeddingAuth = resolveEmbeddingAuth(config);
    this.engine = engineOverride ?? new RememberingEngine({
      dsn: config.dsn,
      schema: config.schema,
      projectDirectory,
      projectId: config.projectId,
      storage: config.storage ? {
        mode: config.storage.mode,
        primary: config.storage.primary,
        httpUrl: config.storage.http.url,
        httpTokenEnv: config.storage.http.tokenEnv,
        httpTimeoutMs: config.storage.http.timeoutMs,
        jsonPath: config.storage.json.path,
      } : { mode: "postgres" },
      embedding: {
        provider: config.embedding.provider as "ollama" | "sentence-transformers" | "hashing",
        model: config.embedding.model,
        host: config.embedding.host,
        ...(embeddingAuth.headers ? { headers: embeddingAuth.headers } : {}),
        ...(embeddingAuth.bearerToken ? { bearerToken: embeddingAuth.bearerToken } : {}),
      },
      retrieval: {
        mode: config.retrieval.mode,
        lexicalK: config.retrieval.lexicalK,
        denseK: config.retrieval.denseK,
        fusionK: config.retrieval.fusionK,
        rerankK: config.retrieval.rerankK,
        reranker: config.retrieval.reranker,
      },
    });
  }

  get schema(): string {
    return this.config.schema;
  }

  async doctor(): Promise<HealthReport> {
    return (await this.engine.nativeDoctor()) as unknown as HealthReport;
  }

  async setup(): Promise<SetupResult> {
    const steps: SetupStep[] = [];
    const timed = async <T>(name: string, fn: () => Promise<T>): Promise<T> => {
      const started = performance.now();
      try {
        const value = await fn();
        steps.push({ name, ok: true, ms: Math.round((performance.now() - started) * 10) / 10, detail: "ok" });
        return value;
      } catch (error) {
        steps.push({
          name, ok: false, ms: Math.round((performance.now() - started) * 10) / 10,
          detail: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200),
        });
        throw error;
      }
    };
    await timed("engine", async () => "native TypeScript engine");
    const setup = await timed("initialise", () => this.engine.setup());
    const refresh = (await timed("refresh", () => this.engine.refresh())) as unknown as RefreshResult;
    return {
      ok: true,
      schema: this.config.schema,
      project_directory: this.projectDirectory,
      steps,
      embedding: setup.embedding,
      refresh,
    };
  }

  async refresh(): Promise<RefreshResult> {
    const report = await this.engine.refresh();
    return {
      ok: report.ok,
      schema: report.schema,
      discovered: report.discovered,
      indexed: report.indexed,
      unchanged: report.unchanged,
      added: report.added,
      changed: report.changed,
      removed: report.removed,
      chunks: report.chunks,
      embedded: report.embedded,
      failed: report.failed,
      ...(report.temporal ? { temporal: report.temporal } : {}),
      ...(report.message ? { message: report.message } : {}),
    };
  }

  async search(query: string, limit = 8): Promise<SearchResult> {
    return (await this.engine.search(query, limit)) as unknown as SearchResult;
  }

  async context(
    query: string,
    maxChars: number,
    maxResults: number,
    route: RouteRequest = "auto",
    temporal?: TemporalStandpointRequest,
    work?: WorkRequest,
    trust?: TrustRequest,
    selection?: SelectionRequest,
  ): Promise<ContextResult> {
    if (route !== "auto" && route !== "recall" && route !== "influence") {
      throw new Error(
        `Invalid route ${JSON.stringify(route)}: expected 'auto', 'recall' or 'influence'.`,
      );
    }
    if (temporal !== undefined) {
      validateTemporalRequest(temporal);
    }
    if (work !== undefined) {
      validateWorkRequest(work);
    }
    if (trust !== undefined) {
      validateTrustRequest(trust);
    }
    if (selection !== undefined) {
      validateSelectionRequest(selection);
    }
    return (await this.engine.context(
      query,
      maxChars,
      maxResults,
      route,
      temporal ? { mode: temporal.mode, valid_at: temporal.valid_at, known_at: temporal.known_at } : undefined,
      work ? {
        mode: work.mode,
        work_type: work.work_type,
        objective: work.objective,
        prior_work_type: work.prior_work_type,
        signals: work.signals?.map((s) => ({
          signal_id: s.signal_id,
          kind: s.kind,
          text: s.text,
          observed_at: s.observed_at,
        })),
      } : undefined,
      trust ? { caller_scope: trust.caller_scope, level: trust.level } : undefined,
      selection ? { mode: selection.mode } : undefined,
    )) as unknown as ContextResult;
  }

  async state(
    subject: string,
    options: {
      query?: string;
      route?: RouteRequest;
      temporal?: TemporalStandpointRequest;
    } = {},
  ): Promise<StateResult> {
    if (!subject.trim()) {
      throw new Error("subject is required and must not be empty");
    }
    const route = options.route ?? "auto";
    if (route !== "auto" && route !== "recall" && route !== "influence") {
      throw new Error(
        `Invalid route ${JSON.stringify(route)}: expected 'auto', 'recall' or 'influence'.`,
      );
    }
    if (options.temporal !== undefined) {
      validateTemporalRequest(options.temporal);
    }
    return (await this.engine.state(subject, {
      query: options.query ?? "",
      route,
      temporal: options.temporal
        ? { mode: options.temporal.mode, valid_at: options.temporal.valid_at, known_at: options.temporal.known_at }
        : undefined,
    })) as unknown as StateResult;
  }

  async temporalImport(): Promise<TemporalImportResult> {
    return (await this.engine.temporalImport()) as unknown as TemporalImportResult;
  }

  async trustImport(): Promise<TemporalImportResult> {
    const result = await this.engine.trustImport();
    return {
      ok: result.ok,
      schema: result.schema,
      message: result.message,
      imported: result.imported,
      duplicates: result.duplicates,
      failed: result.failed,
      events: result.events,
      subjects: result.subjects,
    };
  }

  async trustEval(): Promise<TrustEvaluation> {
    return (await this.engine.trustEval()) as unknown as TrustEvaluation;
  }

  async selectionEval(): Promise<TemporalEvaluation> {
    return (await this.engine.selectionEval()) as unknown as TemporalEvaluation;
  }

  async loopEval(): Promise<LoopEvaluation> {
    return (await this.engine.loopEval()) as unknown as LoopEvaluation;
  }

  async loopImport(): Promise<TemporalImportResult> {
    const result = await this.engine.loopImport();
    return {
      ok: result.ok,
      schema: result.schema,
      message: result.message,
      imported: result.imported,
      duplicates: result.duplicates,
      failed: result.failed,
      events: result.events,
      subjects: result.subjects,
    };
  }

  async loopRebuild(): Promise<Record<string, unknown>> {
    return this.engine.loopRebuild();
  }

  async loopCreate(transition: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (
      !transition ||
      typeof transition !== "object" ||
      Array.isArray(transition)
    ) {
      throw new Error("loop-create requires a transition object.");
    }
    return this.engine.loopCreate(transition);
  }

  async openLoops(request: LoopRequest = {}): Promise<Record<string, unknown>> {
    validateLoopRequest(request);
    return this.engine.openLoops(request);
  }

  async traceEval(): Promise<TemporalEvaluation> {
    return (await this.engine.traceEval()) as unknown as TemporalEvaluation;
  }

  async trace(request: TraceRequest): Promise<Record<string, unknown>> {
    validateTraceRequest(request);
    if (request.mode === "get" || request.mode === "verify") {
      return request.mode === "get"
        ? this.engine.traceGet(request.trace_id)
        : this.engine.traceVerify(request.trace_id);
    }
    if (request.mode === "find") {
      return this.engine.traceFind(request.filters ?? {});
    }
    if (request.mode === "explain") {
      return this.engine.traceExplain(request.trace_id, request.candidate_id);
    }
    if (request.mode === "replay") {
      return this.engine.traceReplay(request.trace_id, request.replay_kind, request.persist ?? false);
    }
    return this.engine.traceDiff(request.trace_id, request.diff_with);
  }

  async temporalEval(): Promise<TemporalEvaluation> {
    return (await this.engine.temporalEval()) as unknown as TemporalEvaluation;
  }

  async frameEval(): Promise<TemporalEvaluation> {
    return (await this.engine.frameEval()) as unknown as TemporalEvaluation;
  }

  async routeEval(): Promise<RouteEvalResult> {
    return (await this.engine.routeEval()) as unknown as RouteEvalResult;
  }

  async remember(request: RememberRequest = {}): Promise<RememberResult> {
    validateRememberRequest(request);
    const output = (await this.engine.remember({
      action: request.action ?? "remember",
      content: request.content,
      targetRecordId: request.target_record_id,
      role: request.role ?? "ordinary",
      reason: request.reason,
      evidenceRefs: request.evidence_refs ?? [],
      effectiveFrom: request.effective_from,
      eventTime: request.event_time,
      idempotencyKey: request.idempotency_key,
      callerScope: request.caller_scope ?? "default",
      origin: request.origin ?? "opencode",
    })) as unknown as {
      ok: boolean;
      code?: string;
      reason?: string;
      action_id?: string;
      action?: string;
      record_id?: string | null;
      target_record_id?: string | null;
      duplicate?: boolean;
      authorization?: { verdict: string; reason: string; policyVersion: string; policyDigest: string; matchedGrantId: string | null };
      record?: { role: string; standingCeiling: string; sourceId: string; lineageRoot: string } | null;
      index?: { indexed: boolean; chunks: number; embedded: number };
      relation?: { type: string; targetRecordId: string } | null;
      schema?: string;
    };
    return {
      ok: output.ok,
      code: output.code,
      reason: output.reason,
      action_id: output.action_id,
      action: output.action,
      record_id: output.record_id,
      target_record_id: output.target_record_id,
      duplicate: output.duplicate,
      authorization: output.authorization ? {
        verdict: output.authorization.verdict,
        reason: output.authorization.reason,
        policy_version: output.authorization.policyVersion,
        policy_digest: output.authorization.policyDigest,
        matched_grant_id: output.authorization.matchedGrantId,
      } : undefined,
      record: output.record ? {
        role: output.record.role,
        standing_ceiling: output.record.standingCeiling,
        source_id: output.record.sourceId,
        lineage_root: output.record.lineageRoot,
      } : null,
      index: output.index,
      relation: output.relation ? {
        type: output.relation.type,
        target_record_id: output.relation.targetRecordId,
      } : null,
      schema: output.schema,
    };
  }

  async writeEval(): Promise<WriteEvaluation> {
    return (await this.engine.writeEval()) as unknown as WriteEvaluation;
  }

  async writeRebuild(): Promise<Record<string, unknown>> {
    return this.engine.writeRebuild();
  }

  async recordShow(recordId: string): Promise<Record<string, unknown>> {
    if (!recordId.trim()) {
      throw new Error("recordShow requires record_id.");
    }
    return this.engine.recordShow(recordId);
  }

  async actionShow(actionId: string): Promise<Record<string, unknown>> {
    if (!actionId.trim()) {
      throw new Error("actionShow requires action_id.");
    }
    return this.engine.actionShow(actionId);
  }

  async writeHistory(recordId: string): Promise<Record<string, unknown>> {
    if (!recordId.trim()) {
      throw new Error("writeHistory requires record_id.");
    }
    return this.engine.writeHistory(recordId);
  }

  async captureSession(
    sessionId: string,
    facts: {
      agent?: string;
      provider?: string;
      model?: string;
      messages: CaptureMessage[];
    },
  ): Promise<CaptureResult> {
    return (await this.engine.captureSession(sessionId, {
      agent: facts.agent,
      provider: facts.provider,
      model: facts.model,
      messages: facts.messages.map((m) => ({
        id: m.id,
        role: m.role,
        text: m.text,
        agent: m.agent,
        model: m.model,
        tool_calls: m.tool_calls,
        tool_results: m.tool_results,
        observed_at: m.observed_at,
      })),
    })) as unknown as CaptureResult;
  }
}
