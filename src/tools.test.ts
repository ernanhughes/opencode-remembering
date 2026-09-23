import { describe, expect, test } from "bun:test";

import type {
  ContextResult,
  HealthReport,
  ProjectMemoryClient,
  RefreshResult,
  SearchResult,
  SetupResult,
} from "./client";
import {
  MemoryContext,
  MemoryHealth,
  MemoryRefresh,
  MemorySearch,
  MemorySetup,
  MemoryState,
} from "./tools";

function fakeClient(overrides: Partial<ProjectMemoryClient>): ProjectMemoryClient {
  return overrides as ProjectMemoryClient;
}

const HEALTH: HealthReport = {
  ok: true,
  engine_root: "C:/Projects/opencode-remembering/engine",
  engine_version: "remembering-engine-v0.1",
  engine_available: true,
  python_dependencies: { psycopg: true },
  dsn_redacted: "postgresql://***:***@localhost:5432/memory",
  schema: "remembering_abc",
  project_directory: "c:\\projects\\x",
  postgres_reachable: true,
  database_exists: true,
  pgvector_available: true,
  pgvector_version: "0.8.0",
  pg_trgm_available: true,
  project_memory_importable: true,
  schema_initialized: true,
  schema_identity_ok: true,
  embedding_provider_reachable: true,
  embedding_model_available: true,
  embedding_detail: "ollama model 'bge-m3' present",
  stored_embedding_version: "ollama:bge-m3:1024",
  stored_embedding_dimension: 1024,
  configured_embedding_version: "ollama:bge-m3",
  embedding_dimension_compatible: true,
  fts_index_present: true,
  hnsw_index_present: true,
  source_count: 3,
  chunk_count: 10,
  indexed: true,
  chunks: 10,
  temporal: {
    store_ready: true,
    store_version: "temporal-store-v0.1",
    event_schema_version: "temporal-event-v0.1",
    reducer_version: "temporal-reducer-v0.1",
    events: 2,
    subjects: ["cache.metadata.database"],
    unknown_references: [],
    causality_violations: [],
    sequence_gaps: [],
  },
  frame: {
    project_frame_present: false,
    project_frame_valid: false,
    project_frame_version: null,
    project_frame_digest: null,
    known_work_types: [],
    framing_engine_version: "framing-engine-v0.1",
    frame_error: null,
  },
  trust: {
    trust_engine_version: "trust-engine-v0.1",
    configured: false,
    policy_source: "builtin_default",
    policy_version: null,
    policy_digest: null,
    policy_valid: true,
    instruction_screen_version: "instruction-screen-v0.1",
    standing_store_ready: false,
    standing_events: 0,
    revoked_sources: [],
    restricted_sources: {},
    policy_error: null,
  },
};

describe("memory tools", () => {
  test("memory_health returns the doctor report", async () => {
    const tool = MemoryHealth(fakeClient({ doctor: async () => HEALTH }));
    const out = await tool.execute!({} as never, {} as never);
    const parsed = JSON.parse(
      (out as { content: string }).content,
    ) as HealthReport;
    expect(parsed.ok).toBe(true);
    expect(parsed.chunk_count).toBe(10);
  });

  test("memory_setup returns the setup report", async () => {
    const setup: SetupResult = {
      ok: true,
      schema: "remembering_abc",
      project_directory: "c:\\x",
      steps: [],
      embedding: {
        provider: "ollama",
        model: "bge-m3",
        dimension: 1024,
        version: "ollama:bge-m3:1024",
      },
      refresh: null,
    };
    const tool = MemorySetup(fakeClient({ setup: async () => setup }));
    const out = await tool.execute!({} as never, {} as never);
    expect(JSON.parse((out as { content: string }).content).ok).toBe(true);
  });

  test("memory_refresh surfaces the refresh report", async () => {
    const refresh: RefreshResult = {
      ok: true,
      schema: "remembering_abc",
      discovered: 5,
      indexed: 0,
      unchanged: 5,
      added: 0,
      changed: 0,
      removed: 0,
      chunks: 0,
      embedded: 0,
      failed: [],
      message: "nothing changed",
    };
    const tool = MemoryRefresh(fakeClient({ refresh: async () => refresh }));
    const out = await tool.execute!({} as never, {} as never);
    const parsed = JSON.parse((out as { content: string }).content);
    expect(parsed.unchanged).toBe(5);
  });

  test("memory_search keeps provenance and trace", async () => {
    const search: SearchResult = {
      ok: true,
      indexed: true,
      schema: "remembering_abc",
      items: [
        {
          chunk_id: "c1",
          source_id: "README.md",
          section: null,
          rank: 1,
          score: 0.9,
          text: "hello",
          lexical_rank: 1,
          dense_rank: 2,
          stage: "fused",
        },
      ],
      trace: null,
    };
    const tool = MemorySearch(
      fakeClient({ search: async () => search }),
    );
    const out = await tool.execute!({ query: "hello" } as never, {} as never);
    const parsed = JSON.parse((out as { content: string }).content);
    expect(parsed.items[0].chunk_id).toBe("c1");
    expect(parsed.items[0].lexical_rank).toBe(1);
  });

  test("memory_context returns bundle fields", async () => {
    const context: ContextResult = {
      ok: true,
      indexed: true,
      schema: "remembering_abc",
      items: [],
      trace: null,
      trace_id: "hybrid:abc",
      content: "evidence",
      chars: 8,
      route: {
        route: "recall",
        route_source: "explicit",
        route_reason: "caller requested route 'recall'",
        route_ambiguous: false,
      },
      temporal: { mode: "current", valid_at: null, known_at: "now" },
      frame: { applied: false, reason: "frame.no_project_frame" },
      trust: {
        mode: "enforce",
        level: "FULL",
        policy_version: "v1",
        policy_source: "builtin_default",
        admitted: 0,
        denied: 0,
        quarantined: 0,
      },
      admission_note: "retrieval evidence only",
    };
    const tool = MemoryContext(
      fakeClient({ context: async () => context }),
    );
    const out = await tool.execute!({ query: "q" } as never, {} as never);
    const parsed = JSON.parse((out as { content: string }).content);
    expect(parsed.trace_id).toBe("hybrid:abc");
    expect(parsed.admission_note).toContain("retrieval");
    expect(parsed.route.route).toBe("recall");
    expect(parsed.route.route_source).toBe("explicit");
    expect(parsed.temporal.mode).toBe("current");
  });

  test("memory_context forwards an explicit route", async () => {
    let seen: string | undefined;
    const tool = MemoryContext(
      fakeClient({
        context: async (_q, _c, _m, route) => {
          seen = route;
          return {
            ok: true,
            indexed: true,
            schema: "s",
            items: [],
            trace: null,
            trace_id: "hybrid:x",
            content: "",
            chars: 0,
            route: {
              route: "recall",
              route_source: "explicit",
              route_reason: "test",
              route_ambiguous: false,
            },
            temporal: { mode: "current", valid_at: null, known_at: null },
            frame: { applied: false, reason: "frame.no_project_frame" },
            trust: {
              mode: "enforce",
              level: "FULL",
              policy_version: "v1",
              policy_source: "builtin_default",
              admitted: 0,
              denied: 0,
              quarantined: 0,
            },
            admission_note: "",
          };
        },
      }),
    );
    await tool.execute!({ query: "q", route: "recall" } as never, {} as never);
    expect(seen).toBe("recall");
  });

  test("memory_context forwards a temporal standpoint", async () => {
    let seenTemporal: unknown;
    const tool = MemoryContext(
      fakeClient({
        context: async (_q, _c, _m, _r, temporal) => {
          seenTemporal = temporal;
          return {
            ok: true,
            indexed: true,
            schema: "s",
            items: [],
            trace: null,
            trace_id: "hybrid:x",
            content: "",
            chars: 0,
            route: {
              route: "recall",
              route_source: "deterministic",
              route_reason: "test",
              route_ambiguous: false,
            },
            temporal: { mode: "valid_at", valid_at: "2026-07-20T00:00:00Z", known_at: null },
            frame: { applied: false, reason: "frame.no_project_frame" },
            trust: {
              mode: "annotate",
              level: "FULL",
              policy_version: "v1",
              policy_source: "builtin_default",
              admitted: 0,
              denied: 0,
              quarantined: 0,
            },
            admission_note: "",
          };
        },
      }),
    );
    await tool.execute!(
      {
        query: "q",
        route: "recall",
        temporal: { mode: "valid_at", valid_at: "2026-07-20T00:00:00Z" },
      } as never,
      {} as never,
    );
    expect(seenTemporal).toEqual({
      mode: "valid_at",
      valid_at: "2026-07-20T00:00:00Z",
    });
  });

  test("memory_context forwards trust options", async () => {
    let seenTrust: unknown;
    const tool = MemoryContext(
      fakeClient({
        context: async (_q, _c, _m, _r, _t, _w, trust) => {
          seenTrust = trust;
          return {
            ok: true,
            indexed: true,
            schema: "s",
            items: [],
            trace: null,
            trace_id: "hybrid:x",
            content: "",
            chars: 0,
            route: {
              route: "influence",
              route_source: "deterministic",
              route_reason: "test",
              route_ambiguous: false,
            },
            temporal: { mode: "current", valid_at: null, known_at: null },
            frame: { applied: false, reason: "frame.no_project_frame" },
            trust: {
              mode: "enforce",
              level: "FULL",
              policy_version: "v1",
              policy_source: "builtin_default",
              admitted: 0,
              denied: 1,
              quarantined: 0,
            },
            admission_note: "",
          };
        },
      }),
    );
    const out = await tool.execute!(
      { query: "q", trust: { caller_scope: "release_agent" } } as never,
      {} as never,
    );
    expect(seenTrust).toEqual({ caller_scope: "release_agent" });
    const parsed = JSON.parse((out as { content: string }).content);
    expect(parsed.trust.denied).toBe(1);
  });

  test("memory_state returns resolved state", async () => {
    const tool = MemoryState(
      fakeClient({
        state: async () => ({
          ok: true,
          schema: "s",
          subject: "cache.metadata.database",
          value: "PostgreSQL",
          status: "OK",
          detail: "valid_from=2026-08-20",
          provenance: ["cache-db-postgres"],
          standpoint: { mode: "current", valid_at: null, known_at: "now" },
          trajectory: [],
          trajectory_truncated: false,
          incomplete_history: false,
          reason: "temporal.current",
          route: {
            route: "influence",
            route_source: "deterministic",
            route_reason: "test",
            route_ambiguous: false,
          },
          store_version: "temporal-store-v0.1",
          event_schema_version: "temporal-event-v0.1",
          reducer_version: "temporal-reducer-v0.1",
        }),
      }),
    );
    const out = await tool.execute!(
      { subject: "cache.metadata.database" } as never,
      {} as never,
    );
    const parsed = JSON.parse((out as { content: string }).content);
    expect(parsed.value).toBe("PostgreSQL");
    expect(parsed.reason).toBe("temporal.current");
  });

  test("bridge failures propagate instead of returning fake results", async () => {
    const failing = fakeClient({
      search: async () => {
        throw new Error("Project Memory bridge failed (search, exit=2): DB_UNREACHABLE: ...");
      },
    });
    const tool = MemorySearch(failing);
    await expect(
      tool.execute!({ query: "x" } as never, {} as never),
    ).rejects.toThrow("DB_UNREACHABLE");
  });
});
