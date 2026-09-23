import { describe, expect, test } from "bun:test";

import { ProjectMemoryClient } from "./client";
import type { RememberingConfig } from "./config";
import type { RememberingEngine } from "./engine/pipeline";

function testConfig(): RememberingConfig {
  return {
    dsn: "postgresql://localhost:5432/x",
    schema: "remembering_abc",
    embedding: { provider: "ollama", model: "bge-m3", host: "http://localhost:11434" },
    retrieval: { mode: "hybrid", lexicalK: 5, denseK: 5, fusionK: 60, rerankK: 5, reranker: "none" },
    context: { autoInject: true, maxChars: 1000, maxResults: 3 },
  };
}

function clientWith(calls: string[], stubs: Record<string, unknown>): ProjectMemoryClient {
  const engine = new Proxy(
    {},
    {
      get: (_target, property: string) => {
        if (property === "then") return undefined;
        return async (...args: unknown[]) => {
          calls.push(property);
          const stub = stubs[property];
          if (typeof stub === "function") return (stub as (...a: unknown[]) => unknown)(...args);
          return stub;
        };
      },
    },
  );
  return new ProjectMemoryClient(testConfig(), process.cwd(), engine as unknown as RememberingEngine);
}

describe("ProjectMemoryClient native delegation", () => {
  test("search delegates without subprocess", async () => {
    const calls: string[] = [];
    const client = clientWith(calls, {
      search: { ok: true, indexed: true, schema: "remembering_abc", items: [], trace: null },
    });
    const result = await client.search("hello", 5);
    expect(calls).toEqual(["search"]);
    expect(result.ok).toBe(true);
  });

  test("remember maps camelCase input to the engine and snake_case output back", async () => {
    const calls: string[] = [];
    let seen: unknown;
    const client = clientWith(calls, {
      remember: async (input: unknown) => {
        seen = input;
        return {
          ok: true, action_id: "act-1", action: "remember", record_id: "rec-1", target_record_id: null,
          authorization: { verdict: "allow", reason: "ok", policyVersion: "v", policyDigest: "d", matchedGrantId: "g" },
          record: { role: "decision", standingCeiling: "FULL", sourceId: "memory://rec-1", lineageRoot: "rec-1" },
          relation: null, index: { indexed: true, chunks: 1, embedded: 1 }, schema: "remembering_abc",
        };
      },
    });
    const result = await client.remember({ action: "remember", content: "OAuth2 decision.", role: "decision" });
    expect((seen as Record<string, unknown>)["targetRecordId"]).toBeUndefined();
    expect((seen as Record<string, unknown>)["callerScope"]).toBe("default");
    expect(result.record_id).toBe("rec-1");
    expect(result.record?.standing_ceiling).toBe("FULL");
    expect(result.record?.lineage_root).toBe("rec-1");
    expect(result.authorization?.policy_version).toBe("v");
  });

  test("context validates route client-side before delegating", async () => {
    const calls: string[] = [];
    const client = clientWith(calls, { context: { ok: true } });
    await expect(client.context("q", 100, 3, "sideways" as never)).rejects.toThrow("Invalid route");
    expect(calls).toEqual([]);
  });

  test("trace fans out to engine operations by mode", async () => {
    const calls: string[] = [];
    const client = clientWith(calls, {
      traceGet: { traceId: "t1" },
      traceVerify: { valid: true, reason: "ok" },
      traceFind: { traces: [] },
      traceExplain: { candidateId: "c1" },
      traceReplay: { persisted: false },
      traceDiff: { onlyInA: [] },
    });
    await client.trace({ mode: "get", trace_id: "t1" });
    await client.trace({ mode: "verify", trace_id: "t1" });
    await client.trace({ mode: "find", filters: {} });
    await client.trace({ mode: "explain", trace_id: "t1", candidate_id: "c1" });
    await client.trace({ mode: "replay", trace_id: "t1", replay_kind: "trust" });
    await client.trace({ mode: "diff", trace_id: "t1", diff_with: "t2" });
    expect(calls).toEqual(["traceGet", "traceVerify", "traceFind", "traceExplain", "traceReplay", "traceDiff"]);
    await expect(client.trace({ mode: "explain", trace_id: "t1", candidate_id: "" })).rejects.toThrow();
  });

  test("loops validate client-side; writes delegate", async () => {
    const calls: string[] = [];
    const client = clientWith(calls, {
      openLoops: { loops: [] },
      loopCreate: { duplicate: false },
      writeRebuild: { ok: true },
      recordShow: { recordId: "rec-1" },
      actionShow: { ok: true },
      writeHistory: { history: [] },
    });
    await expect(client.openLoops({ action: "get" })).rejects.toThrow("loop_id");
    await client.openLoops({ action: "list", state: "open" });
    await client.loopCreate({ event_id: "e", loop_id: "l" });
    await client.writeRebuild();
    await client.recordShow("rec-1");
    await client.actionShow("act-1");
    await client.writeHistory("rec-1");
    expect(calls).toEqual(["openLoops", "loopCreate", "writeRebuild", "recordShow", "actionShow", "writeHistory"]);
    await expect(client.recordShow("  ")).rejects.toThrow();
  });

  test("state and temporal import delegate with standpoint mapping", async () => {
    const calls: string[] = [];
    let stateArgs: unknown;
    const client = clientWith(calls, {
      state: async (...args: unknown[]) => { stateArgs = args; return { ok: true }; },
      temporalImport: { ok: true, imported: 1 },
    });
    await client.state("auth", { route: "recall", temporal: { mode: "valid_at", valid_at: "2026-01-01T00:00:00.000Z" } });
    expect((stateArgs as unknown[])[0]).toBe("auth");
    expect(((stateArgs as unknown[])[1] as Record<string, unknown>)["route"]).toBe("recall");
    await expect(client.state("  ")).rejects.toThrow();
    const imported = await client.temporalImport();
    expect(imported.ok).toBe(true);
    expect(calls).toEqual(["state", "temporalImport"]);
  });
});
