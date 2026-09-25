import type { Pool } from "pg";

import { connectPool } from "../db";
import { EngineError } from "../errors";
import { BaselineStore } from "../storage";
import { PostgresLoopStore } from "../loops/store";
import { PostgresStandingStore } from "../trust/store";
import { PostgresTemporalStore } from "../temporal/store";
import { PostgresTraceStore } from "../trace/store";
import { PostgresWriteStore } from "../write/store";
import { resolveProjectIdentity } from "./project";
import type {
  BaselineStorePort, LoopStorePort, StandingStorePort,
  StorageBackendKind, TemporalStorePort, TraceStorePort, WriteStorePort,
} from "./ports";
import { checkHttpBackend } from "./http/stores";
import {
  HttpBaselineStore, HttpLoopStore, HttpStandingStore, HttpTemporalStore, HttpTraceStore, HttpWriteStore,
} from "./http/stores";
import {
  JsonBaselineStore, JsonLoopStore, JsonStandingStore, JsonTemporalStore, JsonTraceStore, JsonWriteStore,
} from "./json/stores";

export type StorageSelection = {
  activeBackend: StorageBackendKind;
  configuredMode: string;
  primaryBackend: StorageBackendKind | null;
  fallback: boolean;
  /** Which tier served the request when fallback is true. */
  fallbackTier?: "secondary" | "json" | null;
  primaryReachable: boolean | null;
  reason: string | null;
};

export type ResolvedStores = {
  baseline: BaselineStorePort;
  temporal: TemporalStorePort;
  standing: StandingStorePort;
  trace: TraceStorePort;
  loop: LoopStorePort;
  write: WriteStorePort;
  selection: StorageSelection;
  close: () => Promise<void>;
};

export type FactoryOptions = {
  mode: string;
  primary?: "postgres" | "http";
  dsn: string;
  schema: string;
  projectDirectory: string;
  projectId?: string;
  httpUrl: string | null;
  httpTokenEnv?: string;
  httpTimeoutMs: number;
  jsonPath: string;
};

function httpToken(tokenEnv?: string): string | undefined {
  if (!tokenEnv) {
    return process.env.REMEMBERING_HTTP_TOKEN ?? undefined;
  }
  return process.env[tokenEnv] ?? process.env.REMEMBERING_HTTP_TOKEN;
}

async function tryPostgres(dsn: string): Promise<Pool> {
  return connectPool(dsn);
}

async function tryHttp(url: string, tokenEnv: string | undefined, timeoutMs: number, schema: string): Promise<void> {
  await checkHttpBackend({ url, token: httpToken(tokenEnv), timeoutMs }, schema);
}

function primaryFor(opts: FactoryOptions): StorageBackendKind | null {
  if (opts.mode === "postgres" || opts.mode === "http" || opts.mode === "json") return opts.mode;
  if (opts.primary) return opts.primary;
  if (opts.httpUrl) return "http";
  return "postgres";
}

export async function resolveStores(opts: FactoryOptions): Promise<ResolvedStores> {
  const identity = await resolveProjectIdentity(opts.projectDirectory, { explicitId: opts.projectId, schemaOverride: opts.schema });
  const configuredMode = opts.mode;
  const noop = async () => {};

  const makeJson = (): ResolvedStores => {
    const baseline = new JsonBaselineStore(opts.projectDirectory, opts.schema, identity.canonicalPath, identity.projectId, opts.jsonPath);
    return {
      baseline,
      temporal: new JsonTemporalStore(opts.projectDirectory, opts.jsonPath),
      standing: new JsonStandingStore(opts.projectDirectory, opts.jsonPath),
      trace: new JsonTraceStore(opts.projectDirectory, opts.jsonPath),
      loop: new JsonLoopStore(opts.projectDirectory, opts.jsonPath),
      write: new JsonWriteStore(opts.projectDirectory, opts.jsonPath),
      selection: {
        activeBackend: "json", configuredMode, primaryBackend: primaryFor(opts),
        fallback: configuredMode === "auto", fallbackTier: configuredMode === "auto" ? "json" : null,
        primaryReachable: configuredMode === "auto" ? false : null,
        reason: configuredMode === "auto" ? "PRIMARY_UNAVAILABLE_JSON_FALLBACK" : null,
      },
      close: noop,
    };
  };

  if (opts.mode === "json") return makeJson();

  if (opts.mode === "postgres") {
    const pool = await tryPostgres(opts.dsn);
    const close = async () => { await pool.end().catch(() => {}); };
    return {
      baseline: new BaselineStore(pool, opts.dsn, opts.schema),
      temporal: new PostgresTemporalStore(pool, opts.schema),
      standing: new PostgresStandingStore(pool, opts.schema),
      trace: new PostgresTraceStore(pool, opts.schema),
      loop: new PostgresLoopStore(pool, opts.schema),
      write: new PostgresWriteStore(pool, opts.schema),
      selection: { activeBackend: "postgres", configuredMode, primaryBackend: "postgres", fallback: false, primaryReachable: true, reason: null },
      close,
    };
  }

  if (opts.mode === "http") {
    if (!opts.httpUrl) throw new EngineError("CONFIG_INVALID", "http backend requires storage.http.url.");
    const cfg = { url: opts.httpUrl, token: httpToken(opts.httpTokenEnv), timeoutMs: opts.httpTimeoutMs };
    await tryHttp(opts.httpUrl, opts.httpTokenEnv, opts.httpTimeoutMs, opts.schema);
    return {
      baseline: new HttpBaselineStore(cfg, opts.schema),
      temporal: new HttpTemporalStore(cfg, opts.schema),
      standing: new HttpStandingStore(cfg, opts.schema),
      trace: new HttpTraceStore(cfg, opts.schema),
      loop: new HttpLoopStore(cfg, opts.schema),
      write: new HttpWriteStore(cfg, opts.schema),
      selection: { activeBackend: "http", configuredMode, primaryBackend: "http", fallback: false, primaryReachable: true, reason: null },
      close: noop,
    };
  }

  // auto: deterministic order, one authoritative store per operation.
  // If the primary is unreachable but a secondary database backend answers,
  // that is still a fallback: primaryReachable=false keeps health honest.
  const primary = primaryFor(opts) ?? "postgres";
  const order: StorageBackendKind[] = primary === "http"
    ? (opts.httpUrl ? ["http", "postgres"] : ["postgres"])
    : (opts.httpUrl ? ["postgres", "http"] : ["postgres"]);
  let lastError: unknown = null;
  let primaryFailed = false;
  for (const candidate of order) {
    try {
      if (candidate === "postgres") {
        const pool = await tryPostgres(opts.dsn);
        const close = async () => { await pool.end().catch(() => {}); };
        const usedFallback = primaryFailed || candidate !== primary;
        return {
          baseline: new BaselineStore(pool, opts.dsn, opts.schema),
          temporal: new PostgresTemporalStore(pool, opts.schema),
          standing: new PostgresStandingStore(pool, opts.schema),
          trace: new PostgresTraceStore(pool, opts.schema),
          loop: new PostgresLoopStore(pool, opts.schema),
          write: new PostgresWriteStore(pool, opts.schema),
          selection: {
            activeBackend: "postgres", configuredMode, primaryBackend: primary,
            fallback: usedFallback, fallbackTier: usedFallback ? "secondary" : null,
            primaryReachable: !primaryFailed,
            reason: primaryFailed && lastError instanceof EngineError ? lastError.code : null,
          },
          close,
        };
      }
      if (candidate === "http" && opts.httpUrl) {
        const cfg = { url: opts.httpUrl, token: httpToken(opts.httpTokenEnv), timeoutMs: opts.httpTimeoutMs };
        await tryHttp(opts.httpUrl, opts.httpTokenEnv, opts.httpTimeoutMs, opts.schema);
        const usedFallback = primaryFailed || candidate !== primary;
        return {
          baseline: new HttpBaselineStore(cfg, opts.schema),
          temporal: new HttpTemporalStore(cfg, opts.schema),
          standing: new HttpStandingStore(cfg, opts.schema),
          trace: new HttpTraceStore(cfg, opts.schema),
          loop: new HttpLoopStore(cfg, opts.schema),
          write: new HttpWriteStore(cfg, opts.schema),
          selection: {
            activeBackend: "http", configuredMode, primaryBackend: primary,
            fallback: usedFallback, fallbackTier: usedFallback ? "secondary" : null,
            primaryReachable: !primaryFailed,
            reason: primaryFailed && lastError instanceof EngineError ? lastError.code : null,
          },
          close: noop,
        };
      }
    } catch (error) {
      // Security/isolation failures never fall back to a different store.
      const code = (error as { code?: string })?.code;
      if (code === "HTTP_BACKEND_AUTH" || code === "SCHEMA_MISMATCH" || code === "STORE_PROJECT_MISMATCH" || code === "STORE_VERSION_MISMATCH" || code === "CONFIG_INVALID") {
        throw error;
      }
      lastError = error;
      primaryFailed = true;
    }
  }
  const json = makeJson();
  const reason = lastError instanceof EngineError ? lastError.code : "PRIMARY_UNAVAILABLE_JSON_FALLBACK";
  json.selection = { ...json.selection, primaryBackend: primary, fallback: true, fallbackTier: "json", primaryReachable: false, reason };
  return json;
}
