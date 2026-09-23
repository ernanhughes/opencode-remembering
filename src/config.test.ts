import { describe, expect, test } from "bun:test";

import { loadConfig, schemaForProject, validateSchemaName } from "./config";

describe("schemaForProject", () => {
  test("is deterministic for the same directory", async () => {
    const first = await schemaForProject(process.cwd());
    const second = await schemaForProject(process.cwd());
    expect(first).toBe(second);
    expect(first).toMatch(/^remembering_[0-9a-f]{12}$/);
  });

  test("isolates different repositories", async () => {
    const a = await schemaForProject("C:\\Projects\\opencode-remembering");
    const b = await schemaForProject("C:\\Projects\\some-other-project");
    expect(a).not.toBe(b);
  });
});

describe("validateSchemaName", () => {
  test("legacy project_memory_root fails closed", async () => {
    const previous = process.env.PROJECT_MEMORY_ROOT;
    process.env.PROJECT_MEMORY_ROOT = "C:/Projects/project-memory";
    try {
      await expect(loadConfig(process.cwd())).rejects.toThrow(
        /no longer exists/,
      );
    } finally {
      if (previous === undefined) delete process.env.PROJECT_MEMORY_ROOT;
      else process.env.PROJECT_MEMORY_ROOT = previous;
    }
  });  test("accepts safe identifiers", () => {
    expect(validateSchemaName("remembering_abc123")).toBe("remembering_abc123");
    expect(validateSchemaName("baseline")).toBe("baseline");
  });

  test("fails closed on injection attempts", () => {
    expect(() => validateSchemaName("public; DROP TABLE x;--")).toThrow();
    expect(() => validateSchemaName("baseline.chunks")).toThrow();
    expect(() => validateSchemaName('"weird"')).toThrow();
    expect(() => validateSchemaName("")).toThrow();
    expect(() => validateSchemaName("pg_internal")).toThrow();
    expect(() => validateSchemaName("a".repeat(64))).toThrow();
  });
});
