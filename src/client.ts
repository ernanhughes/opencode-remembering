import { spawn } from "node:child_process";

import type { RememberingConfig } from "./config";

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
  project_memory_importable: boolean;
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

export type TemporalStandpointRequest = {
  mode?: "current" | "valid_at" | "as_known" | "bitemporal";
  valid_at?: string;
  known_at?: string;
};

export type TrustRequest = {
  caller_scope?: string;
  level?: "T0" | "S1" | "S2" | "S3" | "FULL";
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

export class ProjectMemoryClient {
  constructor(
    private readonly config: RememberingConfig,
    private readonly projectDirectory: string,
  ) {}

  get schema(): string {
    return this.config.schema;
  }

  private call<T>(action: string, payload: Record<string, unknown> = {}): Promise<T> {
    const input = JSON.stringify({
      ...payload,
      schema: this.config.schema,
      project_directory: this.projectDirectory,
      embedding: {
        provider: this.config.embedding.provider,
        model: this.config.embedding.model,
        host: this.config.embedding.host,
      },
      retrieval: {
        mode: this.config.retrieval.mode,
        lexical_k: this.config.retrieval.lexicalK,
        dense_k: this.config.retrieval.denseK,
        fusion_k: this.config.retrieval.fusionK,
        rerank_k: this.config.retrieval.rerankK,
        reranker: this.config.retrieval.reranker,
      },
    });

    return new Promise<T>((resolve, reject) => {
      const child = spawn(this.config.python, [this.config.bridgePath, action], {
        windowsHide: true,
        env: {
          ...process.env,
          MEMORY_BASELINE_DSN: this.config.dsn,
        },
        stdio: ["pipe", "pipe", "pipe"],
      });

      let stdout = "";
      let stderr = "";
      const timeout = setTimeout(
        () => {
          child.kill();
          reject(new Error(`Project Memory bridge timed out (${action})`));
        },
        TIMEOUTS_MS[action] ?? 60_000,
      );

      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => {
        stdout += chunk;
      });
      child.stderr.on("data", (chunk: string) => {
        stderr += chunk;
      });

      child.on("error", (error) => {
        clearTimeout(timeout);
        reject(
          new Error(
            `Project Memory bridge failed to start (${action}): ${error.message}`,
            { cause: error },
          ),
        );
      });

      child.on("close", (code) => {
        clearTimeout(timeout);
        if (code !== 0) {
          reject(
            new Error(
              `Project Memory bridge failed (${action}, exit=${code}): ${stderr.trim() || stdout.trim()}`,
            ),
          );
          return;
        }

        if (stderr.trim()) {
          console.warn(`[opencode-remembering] bridge stderr: ${stderr.trim()}`);
        }

        try {
          resolve(JSON.parse(stdout) as T);
        } catch (error) {
          reject(
            new Error(
              `Project Memory bridge returned invalid JSON (${action}): ${stdout.slice(0, 500)}`,
              { cause: error },
            ),
          );
        }
      });

      child.stdin.end(input);
    });
  }

  doctor(): Promise<HealthReport> {
    return this.call<HealthReport>("doctor");
  }

  setup(): Promise<SetupResult> {
    return this.call<SetupResult>("setup");
  }

  refresh(): Promise<RefreshResult> {
    return this.call<RefreshResult>("refresh");
  }

  search(query: string, limit = 8): Promise<SearchResult> {
    return this.call<SearchResult>("search", { query, limit });
  }

  context(
    query: string,
    maxChars: number,
    maxResults: number,
    route: RouteRequest = "auto",
    temporal?: TemporalStandpointRequest,
    work?: WorkRequest,
    trust?: TrustRequest,
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
    return this.call<ContextResult>("context", {
      query,
      max_chars: maxChars,
      max_results: maxResults,
      route,
      temporal,
      work,
      trust,
    });
  }

  state(
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
    return this.call<StateResult>("state", {
      subject,
      query: options.query ?? "",
      route,
      temporal: options.temporal,
    });
  }

  temporalImport(): Promise<TemporalImportResult> {
    return this.call<TemporalImportResult>("temporal_import");
  }

  trustImport(): Promise<TemporalImportResult> {
    return this.call<TemporalImportResult>("trust_import");
  }

  trustEval(): Promise<TrustEvaluation> {
    return this.call<TrustEvaluation>("trust_eval");
  }

  temporalEval(): Promise<TemporalEvaluation> {
    return this.call<TemporalEvaluation>("temporal_eval");
  }

  frameEval(): Promise<TemporalEvaluation> {
    return this.call<TemporalEvaluation>("frame_eval");
  }

  routeEval(): Promise<RouteEvalResult> {
    return this.call<RouteEvalResult>("route_eval");
  }

  captureSession(
    sessionId: string,
    facts: {
      agent?: string;
      provider?: string;
      model?: string;
      messages: CaptureMessage[];
    },
  ): Promise<CaptureResult> {
    return this.call<CaptureResult>("capture_session", {
      session_id: sessionId,
      agent: facts.agent,
      provider: facts.provider,
      model: facts.model,
      messages: facts.messages,
    });
  }
}
