import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";

/**
 * Stage 4 cutover guard: the entire TypeScript runtime must not depend on
 * a legacy interpreter, a subprocess bridge, or driver packages that only
 * the old runtime used. The memory engine is native TypeScript.
 */
const RUNTIME_ROOTS = [
  path.join(import.meta.dir),
  path.join(import.meta.dir, "..", "index.ts"),
];

const FORBIDDEN = [
  "remembering_bridge.py",
  "import psycopg",
  "from psycopg",
  "require(\"psycopg",
  "engine/requirements.txt",
  "spawn(",
  "spawnSync(",
  "execFile(",
  "node:child_process",
  "node:child-process",
];

const QUOTED_PYTHON = /["']python(?:\.exe)?["']/;

async function listSourceFiles(roots: string[]): Promise<string[]> {
  const out: string[] = [];
  async function walk(entry: string): Promise<void> {
    const stat = await fs.stat(entry);
    if (stat.isFile()) {
      if (entry.endsWith(".ts") && !entry.endsWith(".test.ts")) out.push(entry);
      return;
    }
    for (const child of await fs.readdir(entry)) {
      await walk(path.join(entry, child));
    }
  }
  for (const root of roots) await walk(root);
  return out;
}

describe("native runtime has no legacy runtime dependency", () => {
  test("src/ and index.ts contain no subprocess/legacy references", async () => {
    const files = await listSourceFiles(RUNTIME_ROOTS);
    expect(files.length).toBeGreaterThan(0);
    const violations: string[] = [];
    for (const file of files) {
      const content = await fs.readFile(file, "utf8");
      for (const token of FORBIDDEN) {
        if (content.includes(token)) violations.push(`${path.relative(process.cwd(), file)}: ${token}`);
      }
      // A quoted 'python' literal in runtime code is a legacy interpreter
      // reference (comments and test fixtures excluded by the file filter).
      if (QUOTED_PYTHON.test(content)) {
        violations.push(`${path.relative(process.cwd(), file)}: quoted python literal`);
      }
    }
    expect(violations).toEqual([]);
  });
});
