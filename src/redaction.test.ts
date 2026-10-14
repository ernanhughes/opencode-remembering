import { describe, expect, test } from "bun:test";
import { isSensitiveField, redactSecrets, redactSensitive, REDACTED_SECRET } from "./redaction";

const fake = "not-a-real-key-abcdef0123456789";

describe("credential redaction", () => {
  test("matches sensitive names but not ordinary token metrics", () => {
    for (const name of ["OPENCODE_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "accessToken", "client_secret", "Authorization", "AWS_SECRET_ACCESS_KEY", "password"]) {
      expect(isSensitiveField(name)).toBe(true);
    }
    for (const name of ["token_count", "prompt_tokens", "monkey", "api_key_help", "source_id"]) {
      expect(isSensitiveField(name)).toBe(false);
    }
  });

  test("scrubs environment assignments, tables and JSON string values", () => {
    const input = [
      "OPENCODE_API_KEY=" + fake,
      "OPENCODE_API_KEY  " + fake,
      "OPENCODE_API_KEY: " + fake,
      '  "OPENCODE_API_KEY": "' + fake + '"',
      "the OPENCODE_API_KEY variable must be set",
    ].join("\n");
    const output = redactSecrets(input);
    expect(output).not.toContain(fake);
    expect(output).toContain("OPENCODE_API_KEY");
    expect(output).toContain("the OPENCODE_API_KEY variable must be set");
    expect(redactSecrets(output)).toBe(output);
  });

  test("scrubs authorization and credential URLs without destroying paths", () => {
    const input = [
      "Authorization: Bearer " + fake,
      "https://username:" + fake + "@example.org/path",
      "https://example.org/items?api_key=" + fake + "&page=2",
    ].join("\n");
    const output = redactSecrets(input);
    expect(output).not.toContain(fake);
    expect(output).toContain("example.org/path");
    expect(output).toContain("page=2");
  });

  test("sanitises nested tool inputs and results without mutating source", () => {
    const source = {
      type: "tool",
      state: {
        input: { path: "README.md", env: { OPENCODE_API_KEY: fake } },
        output: "OPENCODE_API_KEY=" + fake,
        headers: { Authorization: "Bearer " + fake },
      },
    };
    const copy = redactSensitive(source);
    expect(JSON.stringify(copy)).not.toContain(fake);
    expect(copy.state.input.path).toBe("README.md");
    expect(copy.state.input.env.OPENCODE_API_KEY).toBe(REDACTED_SECRET);
    expect(source.state.input.env.OPENCODE_API_KEY).toBe(fake);
    expect(redactSensitive(copy)).toEqual(copy);
  });

  test("handles cyclic tool objects without leaking a secret", () => {
    const cycle: Record<string, unknown> = { token: fake };
    cycle.self = cycle;
    const output = redactSensitive(cycle);
    expect(JSON.stringify(output)).not.toContain(fake);
    expect((output as Record<string, unknown>).self).toBe(REDACTED_SECRET);
  });
});

test("preserves ordinary authentication prose and adjacent JSON fields", () => {
  const normal = "Basic Authentication is supported";
  expect(redactSecrets(normal)).toBe(normal);
  const fake = "not-a-real-key-abcdef0123456789";
  const input = '{"headers":{"Authorization":"Bearer ' + fake + '"},"status":"ok"}';
  const output = redactSecrets(input);
  expect(output).not.toContain(fake);
  expect(output).toContain('"status":"ok"');
  expect(redactSecrets(output)).toBe(output);
});
