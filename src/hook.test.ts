import { describe, expect, test } from "bun:test";

import type { ContextResult, ProjectMemoryClient } from "./client";
import type { RememberingConfig } from "./config";
import { buildContextHook, extractWorkSignals, isSecurityFailure } from "./hook";

const BASE_CONFIG = {
  context: { autoInject: true, maxChars: 4000, maxResults: 6 },
} as RememberingConfig;

function clientWith(overrides: Partial<ProjectMemoryClient>): ProjectMemoryClient {
  return overrides as ProjectMemoryClient;
}

function eventWith(messages: unknown[]) {
  return { messages, system: [] as Array<{ type: string; text: string }> };
}

function bundleWith(
  route: "recall" | "influence",
  ambiguous = false,
): ContextResult {
  return {
    ok: true,
    indexed: true,
    schema: "remembering_abc",
    items: [],
    trace: null,
    trace_id: "hybrid:1",
    content: "evidence text",
    chars: 13,
    route: {
      route,
      route_source: "deterministic",
      route_reason: "test",
      route_ambiguous: ambiguous,
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
    selection: {
      policy_version: "decisive-selection-v0.1",
      input_count: 0,
      selected_count: 0,
      dropped_redundant: 0,
      dropped_low_value: 0,
      dropped_budget: 0,
      chars_before: 0,
      chars_after: 0,
      compression_ratio: 1.0,
      budget_insufficient: false,
    },
    admission_note: "retrieval evidence only",
    trace_persisted: true,
  };
}

describe("context hook", () => {
  test("injects a bounded bundle after capture", async () => {
    const seen: string[] = [];
    const client = clientWith({
      captureSession: async () => {
        seen.push("capture");
        return {
          ok: true,
          schema: "s",
          session_id: "ses_1",
          transcript_path: "p",
          recorded: 1,
          duplicates: 0,
          total: 1,
        };
      },
      context: async () => bundleWith("influence"),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { sessionID: "x", role: "user", content: "how does setup work?" },
    ]);
    (event as { sessionID?: string }).sessionID = "ses_1";
    await hook(event);
    expect(seen).toEqual(["capture"]);
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain("hybrid:1");
  });

  test("recall request injects a recall bundle", async () => {
    let requestedRoute = "";
    const client = clientWith({
      context: async (_q, _c, _m, route) => {
        requestedRoute = route ?? "";
        return bundleWith("recall");
      },
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { role: "user", content: "Where did we discuss pgvector?" },
    ]);
    await hook(event);
    expect(requestedRoute).toBe("auto");
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain('route="recall"');
  });

  test("action request injects an influence bundle", async () => {
    const client = clientWith({
      context: async () => bundleWith("influence"),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { role: "user", content: "Fix the current migration now please" },
    ]);
    await hook(event);
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain('route="influence"');
  });

  test("ambiguous request uses the conservative policy visibly", async () => {
    const client = clientWith({
      context: async () => bundleWith("influence", true),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([{ role: "user", content: "Memory architecture" }]);
    await hook(event);
    // Ambiguity is observable in the bundle, not hidden: injection still
    // happens under the influence contract.
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain('route="influence"');
  });

  test("injected wrapper carries route and temporal mode", async () => {
    const client = clientWith({
      context: async () => ({
        ...bundleWith("influence"),
        temporal: {
          mode: "current",
          valid_at: null,
          known_at: "2026-09-01T00:00:00Z",
        },
      }),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { role: "user", content: "Which database should we use now?" },
    ]);
    await hook(event);
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain('route="influence"');
    expect(event.system[0]!.text).toContain('temporal_mode="current"');
  });

  test("no injection when the store is uninitialized", async () => {
    const client = clientWith({
      captureSession: async () => ({
        ok: true,
        schema: "s",
        session_id: "ses_1",
        transcript_path: "p",
        recorded: 0,
        duplicates: 1,
        total: 1,
      }),
      context: async () => ({
        ...bundleWith("influence"),
        indexed: false,
        trace_id: "hybrid:unindexed",
        content: "",
        chars: 0,
      }),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([{ role: "user", content: "real question here" }]);
    await hook(event);
    expect(event.system).toHaveLength(0);
  });

  test("no injection for trivial queries, but capture still runs", async () => {
    let captured = 0;
    let contextCalls = 0;
    const client = clientWith({
      captureSession: async () => {
        captured += 1;
        return {
          ok: true,
          schema: "s",
          session_id: "ses_1",
          transcript_path: "p",
          recorded: 0,
          duplicates: 0,
          total: 0,
        };
      },
      context: async () => {
        contextCalls += 1;
        throw new Error("must not be called");
      },
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([{ role: "user", content: "hi" }]);
    (event as { sessionID?: string }).sessionID = "ses_1";
    await hook(event);
    expect(captured).toBe(1);
    expect(contextCalls).toBe(0);
    expect(event.system).toHaveLength(0);
  });

  test("a failed memory lookup never breaks the session", async () => {
    const client = clientWith({
      captureSession: async () => {
        throw new Error("disk full (simulated)");
      },
      context: async () => {
        throw new Error("DB_UNREACHABLE: cannot reach PostgreSQL");
      },
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([{ role: "user", content: "real question here" }]);
    (event as { sessionID?: string }).sessionID = "ses_1";
    await hook(event);
    expect(event.system).toHaveLength(0);
  });

  test("empty memory content is not injected", async () => {
    const client = clientWith({
      context: async () => ({
        ...bundleWith("influence"),
        trace_id: "hybrid:2",
        content: "   ",
        chars: 3,
      }),
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([{ role: "user", content: "real question here" }]);
    await hook(event);
    expect(event.system).toHaveLength(0);
  });
});

describe("isSecurityFailure", () => {
  test("detects isolation failures", () => {
    expect(isSecurityFailure(new Error("SCHEMA_MISMATCH: ..."))).toBe(true);
    expect(isSecurityFailure(new Error("FRAME_PROJECT_MISMATCH: ..."))).toBe(
      true,
    );
    expect(isSecurityFailure(new Error("CONFIG_INVALID: ..."))).toBe(true);
    expect(isSecurityFailure(new Error("DB_UNREACHABLE"))).toBe(false);
  });
});

describe("extractWorkSignals", () => {
  const NOW = "2026-09-24T00:00:00Z";

  test("builds user, agent, tool, and test-failure signals", () => {
    const signals = extractWorkSignals(
      { messages: [], system: [] },
      {
        sessionId: "ses_9",
        agent: "build",
        messages: [
          { role: "user", text: "Fix the migration." },
          {
            role: "assistant",
            text: "",
            tool_calls: [],
            tool_results: [{ type: "text", text: "migration test FAILED" }],
          },
        ],
      },
      NOW,
    );
    const kinds = signals.map((s) => s.kind);
    expect(kinds).toEqual([
      "user_message",
      "agent_task",
      "tool_result",
      "test_failure",
    ]);
    expect(signals[0]!.signal_id).toContain("ses_9");
    expect(signals.every((s) => s.observed_at === NOW)).toBe(true);
  });

  test("canonical history is not dumped into signals", () => {
    const signals = extractWorkSignals(
      { messages: [], system: [] },
      {
        messages: [
          { role: "user", text: "older question" },
          { role: "assistant", text: "older answer" },
          { role: "user", text: "current request" },
        ],
      },
      NOW,
    );
    expect(signals).toHaveLength(1);
    expect(signals[0]!.text).toBe("current request");
  });

  test("hook passes caller scope and renders trust policy", async () => {
    let seenTrust: unknown;
    const client = clientWith({
      captureSession: async () => ({
        ok: true,
        schema: "s",
        session_id: "ses_7",
        transcript_path: "p",
        recorded: 0,
        duplicates: 0,
        total: 0,
      }),
      context: async (_q, _c, _m, _r, _t, _w, trust) => {
        seenTrust = trust;
        return {
          ...bundleWith("influence"),
          trust: {
            mode: "enforce",
            level: "FULL",
            policy_version: "v1",
            policy_source: "builtin_default",
            admitted: 1,
            denied: 0,
            quarantined: 0,
          },
        };
      },
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { role: "user", content: "Fix the migration now" },
    ]);
    (event as { sessionID?: string }).sessionID = "ses_7";
    (event as { agent?: string }).agent = "build";
    await hook(event);
    // Agent identity becomes the caller scope; default otherwise.
    expect((seenTrust as { caller_scope: string }).caller_scope).toBe(
      "build",
    );
    expect(event.system[0]!.text).toContain('trust_policy="v1"');
  });

  test("hook passes auto work signals and renders frame attrs", async () => {
    let seenWork: unknown;
    const client = clientWith({
      context: async (_q, _c, _m, _r, _t, work) => {
        seenWork = work;
        return {
          ...bundleWith("influence"),
          frame: {
            applied: true,
            reason: "frame.declared",
            control: "hard" as const,
            establishment: {
              establishment: "declared",
              work_type: "implementation",
              objective: "Fix the migration.",
              supporting_refs: ["signal-user-ses_1"],
              conflicting_refs: [],
              reasons: ["frame.declared"],
              source: "deterministic",
              prior_work_type: null,
            },
          },
        };
      },
    });
    const hook = buildContextHook(client, BASE_CONFIG);
    const event = eventWith([
      { role: "user", content: "Fix the migration now" },
    ]);
    (event as { sessionID?: string }).sessionID = "ses_1";
    await hook(event);
    expect((seenWork as { mode: string }).mode).toBe("auto");
    expect(
      (seenWork as { signals: Array<{ kind: string }> }).signals[0]!.kind,
    ).toBe("user_message");
    expect(event.system).toHaveLength(1);
    expect(event.system[0]!.text).toContain('work_type="implementation"');
    expect(event.system[0]!.text).toContain('frame_establishment="declared"');
    expect(event.system[0]!.text).toContain('frame_control="hard"');
  });
});
