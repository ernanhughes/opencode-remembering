import { describe, expect, test } from "bun:test";

import { latestUserText, renderInjectedMemory } from "./context";

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

describe("renderInjectedMemory", () => {
  test("keeps trace and schema visible", () => {
    const rendered = renderInjectedMemory("bootstrap:abc", "remembering_deadbeef", "evidence");
    expect(rendered).toContain('trace_id="bootstrap:abc"');
    expect(rendered).toContain('schema="remembering_deadbeef"');
    expect(rendered).toContain("evidence");
  });
});
