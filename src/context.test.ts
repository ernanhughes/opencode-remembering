import { describe, expect, test } from "bun:test";

import {
  extractCaptureMessages,
  extractSessionFacts,
  isMeaningfulQuery,
  latestUserText,
  renderInjectedMemory,
} from "./context";

describe("latestUserText", () => {
  test("finds the latest user text across common message shapes", () => {
    const messages = [
      { role: "user", content: "older" },
      { role: "assistant", content: "reply" },
      {
        info: { role: "user" },
        parts: [{ type: "text", text: "new request" }],
      },
    ];

    expect(latestUserText(messages)).toBe("new request");
  });

  test("returns empty when no user text is available", () => {
    expect(latestUserText([{ role: "assistant", content: "hello" }])).toBe("");
  });
});

describe("isMeaningfulQuery", () => {
  test("rejects empty and trivial queries", () => {
    expect(isMeaningfulQuery("")).toBe(false);
    expect(isMeaningfulQuery("  ")).toBe(false);
    expect(isMeaningfulQuery("hi")).toBe(false);
    expect(isMeaningfulQuery("...")).toBe(false);
  });

  test("accepts real work signals", () => {
    expect(isMeaningfulQuery("how does setup work?")).toBe(true);
    expect(isMeaningfulQuery("fix the bug")).toBe(true);
  });
});

describe("renderInjectedMemory", () => {
  test("keeps trace and schema visible", () => {
    const rendered = renderInjectedMemory(
      "hybrid:abc",
      "remembering_deadbeef",
      "evidence",
      "influence",
    );
    expect(rendered).toContain('trace_id="hybrid:abc"');
    expect(rendered).toContain('schema="remembering_deadbeef"');
    expect(rendered).toContain('route="influence"');
    expect(rendered).toContain("evidence");
  });

  test("states the retrieval-vs-influence boundary", () => {
    const rendered = renderInjectedMemory("hybrid:abc", "s", "evidence", "recall");
    expect(rendered).toMatch(/retrieval/i);
    expect(rendered).toContain('route="recall"');
  });
});

describe("extractCaptureMessages", () => {
  test("captures role, text and message ids", () => {
    const messages = [
      { id: "m1", role: "user", parts: [{ type: "text", text: "hello" }] },
      { role: "assistant", content: "hi there" },
    ];
    const records = extractCaptureMessages(messages);
    expect(records).toHaveLength(2);
    expect(records[0]!).toMatchObject({ id: "m1", role: "user", text: "hello" });
    expect(records[1]!).toMatchObject({ role: "assistant", text: "hi there" });
  });

  test("collects tool-call and tool-result parts", () => {
    const messages = [
      {
        role: "assistant",
        parts: [
          {
            type: "tool-call",
            toolCallId: "t1",
            toolName: "read",
            input: { path: "x" },
          },
        ],
      },
      {
        role: "tool",
        parts: [{ type: "tool-result", toolCallId: "t1", output: "ok" }],
      },
    ];
    const records = extractCaptureMessages(messages);
    expect(records[0]!.tool_calls).toHaveLength(1);
    expect(records[1]!.tool_results).toHaveLength(1);
  });

  test("drops empty messages", () => {
    expect(extractCaptureMessages([{}, { role: "", content: "" }])).toEqual([]);
  });
});

describe("extractSessionFacts", () => {
  test("extracts session id, agent and model ref", () => {
    const facts = extractSessionFacts({
      sessionID: "ses_123",
      agent: "build",
      model: { providerID: "ollama", modelID: "qwen3" },
      messages: [{ role: "user", content: "do it" }],
    });
    expect(facts.sessionId).toBe("ses_123");
    expect(facts.agent).toBe("build");
    expect(facts.provider).toBe("ollama");
    expect(facts.model).toBe("qwen3");
    expect(facts.messages).toHaveLength(1);
  });

  test("handles missing event gracefully", () => {
    expect(extractSessionFacts(null)).toEqual({ messages: [] });
    expect(extractSessionFacts({})).toEqual({
      sessionId: undefined,
      agent: undefined,
      provider: undefined,
      model: undefined,
      messages: [],
    });
  });
});
