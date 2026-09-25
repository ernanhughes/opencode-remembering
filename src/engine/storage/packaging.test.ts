import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";

/**
 * Packaging proof for pg-optionality: JSON/HTTP installs must not require the
 * `pg` driver at runtime. Stronger than observing the dev install (where
 * optionalDependencies may still be present): assert the dependency metadata
 * AND that no shippable source file statically imports the driver.
 */

const ROOT = path.join(import.meta.dir, "..", "..", "..");

async function listTsFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules") continue;
      out.push(...(await listTsFiles(full)));
    } else if (entry.isFile() && entry.name.endsWith(".ts") && !entry.name.endsWith(".test.ts")) {
      out.push(full);
    }
  }
  return out;
}

describe("pg-absent packaging", () => {
  test("pg is optional, never mandatory", async () => {
    const pkg = JSON.parse(await fs.readFile(path.join(ROOT, "package.json"), "utf8")) as {
      dependencies?: Record<string, string>;
      optionalDependencies?: Record<string, string>;
    };
    expect(pkg.dependencies ?? {}).not.toHaveProperty("pg");
    expect(pkg.optionalDependencies ?? {}).toHaveProperty("pg");
  });

  test("no shippable source statically imports the pg driver", async () => {
    const files = await listTsFiles(path.join(ROOT, "src"));
    const violations: string[] = [];
    for (const file of files) {
      const content = await fs.readFile(file, "utf8");
      for (const line of content.split("\n")) {
        const trimmed = line.trim();
        if (trimmed.startsWith("//") || trimmed.startsWith("*")) continue;
        // `import type` is erased at compile time and is safe.
        if (/^\s*import\s+type\b/.test(line)) continue;
        if (/^\s*import\s+[^'"]*\sfrom\s+["']pg["']/.test(line)) {
          violations.push(`${path.relative(ROOT, file)}: static value import from "pg"`);
        }
        if (/require\(\s*["']pg["']\s*\)/.test(line)) {
          violations.push(`${path.relative(ROOT, file)}: require("pg")`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  test("the sole runtime pg load is a guarded lazy import with guidance", async () => {
    const db = await fs.readFile(path.join(ROOT, "src", "engine", "db.ts"), "utf8");
    expect(db).toContain('await import("pg")');
    expect(db).toContain("optional 'pg' driver is not installed");
    expect(db).toContain("DB_UNREACHABLE");
  });
});
