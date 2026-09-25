import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";

/**
 * Client RPC ↔ SQL parity: every rpc(..., "remembering_*", body) in the HTTP
 * backend must have a matching CREATE FUNCTION with argument names exactly
 * equal to the JSON body keys (PostgREST matches request keys to argument
 * names). This test makes client/SQL drift mechanically impossible.
 */

const STORES_TS = path.join(import.meta.dir, "stores.ts");
const SQL_FILE = path.join(import.meta.dir, "..", "..", "..", "..", "sql", "remembering-http-v1.sql");

type RpcCall = { name: string; keys: string[] };

function extractRpcCalls(source: string): RpcCall[] {
  const calls: RpcCall[] = [];
  // Scan for `rpc`, then skip an optional balanced generic argument list
  // (generics nest: rpc<{ items: Array<Record<string, unknown>> }>), then
  // read (cfg, "name", {body}).
  const rpcRe = /\brpc\b/g;
  let rpcMatch: RegExpExecArray | null;
  while ((rpcMatch = rpcRe.exec(source))) {
    let i = rpcMatch.index + 3;
    const skipWs = () => { while (i < source.length && /\s/.test(source[i] as string)) i++; };
    skipWs();
    if (source[i] === "<") {
      let depth = 0;
      let s: string | null = null;
      let p = "";
      for (; i < source.length; i++) {
        const ch = source[i] as string;
        if (s) {
          if (ch === s && p !== "\\") s = null;
        } else if (ch === '"' || ch === "'" || ch === "`") {
          s = ch;
        } else if (ch === "<") {
          depth += 1;
        } else if (ch === ">") {
          depth -= 1;
          if (depth === 0) { i++; break; }
        }
        p = ch;
      }
      skipWs();
    }
    const head = /\(\s*(?:this\.cfg|cfg)\s*,\s*"([^"]+)"\s*,/.exec(source.slice(i, i + 120));
    if (!head) continue;
    const name = head[1] as string;
    // Find the opening brace of the body object after the match.
    const braceStart = source.indexOf("{", i + head[0].length);
    if (braceStart === -1) throw new Error(`no body object for rpc ${name}`);
    // Scan with depth tracking to find the matching close brace.
    let bdepth = 0;
    let j = braceStart;
    let inString: string | null = null;
    let prev = "";
    for (; j < source.length; j++) {
      const ch = source[j] as string;
      if (inString) {
        if (ch === inString && prev !== "\\") inString = null;
      } else if (ch === '"' || ch === "'" || ch === "`") {
        inString = ch;
      } else if (ch === "{") {
        bdepth += 1;
      } else if (ch === "}") {
        bdepth -= 1;
        if (bdepth === 0) break;
      }
      prev = ch;
    }
    const body = source.slice(braceStart + 1, j);
    // Split into top-level comma segments (depth- and string-aware), then
    // take each segment's leading identifier. Handles both `key: value`
    // and shorthand `key` properties.
    const keys: string[] = [];
    let d = 0;
    let s: string | null = null;
    let p = "";
    let segment = "";
    const segments: string[] = [];
    for (let j = 0; j < body.length; j++) {
      const ch = body[j] as string;
      if (s) {
        if (ch === s && p !== "\\") s = null;
        segment += ch;
      } else if (ch === '"' || ch === "'" || ch === "`") {
        s = ch;
        segment += ch;
      } else if (ch === "{" || ch === "[") {
        d += 1;
        segment += ch;
      } else if (ch === "}" || ch === "]") {
        d -= 1;
        segment += ch;
      } else if (ch === "," && d === 0) {
        segments.push(segment);
        segment = "";
      } else {
        segment += ch;
      }
      p = ch;
    }
    segments.push(segment);
    for (const seg of segments) {
      const keyMatch = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::|$)/.exec(seg);
      if (keyMatch) keys.push(keyMatch[1] as string);
    }
    calls.push({ name, keys });
  }
  return calls;
}

function extractSqlFunctions(sql: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const fnRe = /CREATE\s+OR\s+REPLACE\s+FUNCTION\s+(remembering_\w+)\s*\(([\s\S]*?)\)\s*RETURNS/gi;
  let match: RegExpExecArray | null;
  while ((match = fnRe.exec(sql))) {
    const name = match[1] as string;
    const argsRaw = (match[2] as string).trim();
    if (!argsRaw) {
      out.set(name, []);
      continue;
    }
    // Argument types in this artifact never contain commas, so a flat split
    // is exact. Names may be quoted ("timestamp", "query", "key", ...).
    const args = argsRaw.split(",").map((a) => {
      const first = a.trim().split(/\s+/)[0] as string;
      return first.replace(/^"|"$/g, "");
    });
    out.set(name, args);
  }
  return out;
}

describe("HTTP RPC ↔ SQL parity", () => {
  test("every client RPC exists in SQL and vice versa", async () => {
    const source = await fs.readFile(STORES_TS, "utf8");
    const sql = await fs.readFile(SQL_FILE, "utf8");
    const calls = extractRpcCalls(source);
    expect(calls.length).toBeGreaterThan(30);
    const sqlFns = extractSqlFunctions(sql);
    const clientNames = new Set(calls.map((c) => c.name));
    // remembering_assert_schema is a SQL-side helper, not an HTTP RPC.
    sqlFns.delete("remembering_assert_schema");
    const sqlNames = new Set(sqlFns.keys());
    expect([...clientNames].sort()).toEqual([...sqlNames].sort());
  });

  test("SQL argument names exactly match client JSON body keys", async () => {
    const source = await fs.readFile(STORES_TS, "utf8");
    const sql = await fs.readFile(SQL_FILE, "utf8");
    const calls = extractRpcCalls(source);
    const sqlFns = extractSqlFunctions(sql);
    const problems: string[] = [];
    // Multiple call sites may use the same RPC with identical keys; check all.
    for (const call of calls) {
      const args = sqlFns.get(call.name);
      if (!args) {
        problems.push(`${call.name}: missing from SQL`);
        continue;
      }
      const want = [...call.keys].sort().join(",");
      const got = [...args].sort().join(",");
      if (want !== got) {
        problems.push(`${call.name}: client keys {${want}} != SQL args {${got}}`);
      }
    }
    expect(problems).toEqual([]);
  });

  test("checkHttpBackend probes the same health RPC the SQL artifact defines", async () => {
    const source = await fs.readFile(STORES_TS, "utf8");
    expect(source).toContain(`rpc<{ ok: boolean }>(cfg, "remembering_health", { schema })`);
  });
});
