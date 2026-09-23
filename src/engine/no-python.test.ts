import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";

const ENGINE_DIR = path.join(import.meta.dir);
const FORBIDDEN = [
  "remembering_bridge.py",
  "from psycopg",
  "import psycopg",
  "engine/requirements.txt",
  "spawn(",
  "child_process",
];

async function listTsFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await listTsFiles(full)));
    else if (entry.isFile() && entry.name.endsWith(".ts") && !entry.name.endsWith(".test.ts")) {
      out.push(full);
    }
  }
  return out;
}

describe("native engine has no Python runtime dependency", () => {
  test("src/engine contains no bridge/spawn/psycopg references", async () => {
    const files = await listTsFiles(ENGINE_DIR);
    expect(files.length).toBeGreaterThan(0);
    const violations: string[] = [];
    for (const file of files) {
      const content = await fs.readFile(file, "utf8");
      for (const token of FORBIDDEN) {
        if (content.includes(token)) violations.push(`${path.basename(file)}: ${token}`);
      }
      if (/["']python["']/.test(content) && content.includes("provider")) {
        violations.push(`${path.basename(file)}: suspicious 'python' literal`);
      }
    }
    expect(violations).toEqual([]);
  });
});
