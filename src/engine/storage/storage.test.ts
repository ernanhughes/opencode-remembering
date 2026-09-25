import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS = "1";

import { HashingEmbedder } from "../embeddings";
import { RememberingEngine } from "../pipeline";
import { resolveStores } from "./factory";
import { JsonBaselineStore } from "./json/stores";
import { bm25Scores } from "./json/lexical";
import { HttpBaselineStore } from "./http/stores";
import { redactSecrets } from "../errors";

async function tempDir(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), "remembering-json-"));
}

describe("JSON backend", () => {
  let dir: string;
  beforeEach(async () => { dir = await tempDir(); });
  afterEach(async () => { await fs.rm(dir, { recursive: true, force: true }); });

  test("initialises, isolates projects, and persists across restarts", async () => {
    const a = new JsonBaselineStore(dir, "remembering_abc", dir.toLowerCase(), "local:remembering_abc", ".remembering/store");
    await a.initialise(16);
    await a.upsertSource("doc.md", "document", "hash1", null);
    await a.replaceChunks("doc.md", [{
      chunkId: "c1", sourceId: "doc.md", ordinal: 0, text: "hello world project memory",
      section: null, charStart: 0, charEnd: 10, contentHash: "h", chunker: "t:0.1.0",
      embeddingVersion: "hashing:16", embedding: new Array(16).fill(0.25),
    }]);
    const again = new JsonBaselineStore(dir, "remembering_abc", dir.toLowerCase(), "local:remembering_abc", ".remembering/store");
    const known = await again.knownSources();
    expect(known["doc.md"]).toBe("hash1");
    expect(await again.orphanChunks()).toBe(0);
  });

  test("project mismatch fails closed", async () => {
    const a = new JsonBaselineStore(dir, "remembering_abc", "/proj/a", "local:a", ".remembering/store");
    await a.initialise(8);
    const b = new JsonBaselineStore(dir, "remembering_abc", "/proj/b", "local:b", ".remembering/store");
    expect(b.initialise(8)).rejects.toThrow("STORE_PROJECT_MISMATCH");
  });

  test("lexical ranking prefers term matches over non-matches", async () => {
    const scores = bm25Scores("migration validation", [
      { chunkId: "a", text: "migration validation must run before deploy" },
      { chunkId: "b", text: "unrelated cooking recipe" },
    ]);
    expect(scores.get("a") ?? 0).toBeGreaterThan(scores.get("b") ?? 0);
    expect(scores.has("b")).toBe(false);
  });

  test("engine refresh/search/remember work end-to-end on JSON without postgres", async () => {
    const project = await tempDir();
    try {
      await fs.writeFile(path.join(project, "notes.md"), "# Notes\nMigration validation must run before deploy.\n");
      const engine = new RememberingEngine({
        dsn: "postgresql://127.0.0.1:1/nope",
        schema: "remembering_json_e2e",
        projectDirectory: project,
        projectId: "test:e2e",
        storage: { mode: "json", jsonPath: ".remembering/store" },
        embedding: { provider: "hashing", model: "hashing-16", host: "http://localhost:11434", dimension: 16 },
        retrieval: { mode: "hybrid", lexicalK: 10, denseK: 10, fusionK: 60, rerankK: 5, reranker: "none" },
      });
      engine.withEmbedder(new HashingEmbedder(16));
      try {
        await engine.setup();
        // Exclude the JSON store itself: refresh must not ingest it.
        const report = await engine.refresh();
        expect(report.discovered).toBeGreaterThan(0);
        const search = await engine.search("migration validation", 5);
        expect(search.indexed).toBe(true);
        expect(search.items.length).toBeGreaterThan(0);
        const remember = await engine.remember({ action: "remember", content: "Migration validation is required.", callerScope: "test", origin: "cli" });
        expect(remember["ok"]).toBe(true);
        const search2 = await engine.search("migration validation", 5);
        expect(search2.items.some((i) => i.source_id.startsWith("memory://"))).toBe(true);
        const health = await engine.nativeDoctor();
        expect(health["ok"]).toBe(true);
        expect((health["storage"] as { activeBackend: string }).activeBackend).toBe("json");
      } finally {
        await engine.close();
      }
    } finally {
      await fs.rm(project, { recursive: true, force: true });
    }
  });

  test("JSON store never ingests itself", async () => {
    const project = await tempDir();
    try {
      await fs.writeFile(path.join(project, "a.md"), "hello memory world");
      const engine = new RememberingEngine({
        dsn: "postgresql://127.0.0.1:1/nope",
        schema: "remembering_json_self",
        projectDirectory: project,
        storage: { mode: "json", jsonPath: ".remembering/store" },
        embedding: { provider: "hashing", model: "hashing-16", host: "http://localhost:11434", dimension: 16 },
        retrieval: { mode: "hybrid", lexicalK: 10, denseK: 10, fusionK: 60, rerankK: 5, reranker: "none" },
      });
      engine.withEmbedder(new HashingEmbedder(16));
      try {
        await engine.setup();
        await engine.refresh();
        const resolved = await resolveStores({
          mode: "json", dsn: "", schema: "remembering_json_self", projectDirectory: project,
          httpUrl: null, httpTimeoutMs: 5000, jsonPath: ".remembering/store",
        });
        const known = await resolved.baseline.knownSources();
        expect(Object.keys(known).some((k) => k.includes("store/"))).toBe(false);
        await resolved.close();
      } finally {
        await engine.close();
      }
    } finally {
      await fs.rm(project, { recursive: true, force: true });
    }
  });
});

describe("backend selection", () => {
  test("explicit json never touches postgres", async () => {
    const dir = await tempDir();
    try {
      const resolved = await resolveStores({
        mode: "json", dsn: "postgresql://127.0.0.1:1/nope", schema: "remembering_x",
        projectDirectory: dir, httpUrl: null, httpTimeoutMs: 1000, jsonPath: ".remembering/store",
      });
      expect(resolved.selection.activeBackend).toBe("json");
      await resolved.close();
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  test("auto falls back to json when postgres is unreachable", async () => {
    const dir = await tempDir();
    try {
      const resolved = await resolveStores({
        mode: "auto", dsn: "postgresql://127.0.0.1:1/nope", schema: "remembering_x",
        projectDirectory: dir, httpUrl: null, httpTimeoutMs: 1000, jsonPath: ".remembering/store",
      });
      expect(resolved.selection.activeBackend).toBe("json");
      expect(resolved.selection.fallback).toBe(true);
      expect(resolved.selection.fallbackTier).toBe("json");
      expect(resolved.selection.primaryReachable).toBe(false);
      await resolved.close();
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  test("auto secondary win is still reported as fallback", async () => {
    // Primary postgres is unreachable; the HTTP secondary answers.
    // Diagnostics must show fallback:true with the primary marked unreachable.
    const original = globalThis.fetch;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => Response.json({ ok: true })) as unknown as typeof fetch;
    const dir = await tempDir();
    try {
      const resolved = await resolveStores({
        mode: "auto", primary: "postgres", dsn: "postgresql://127.0.0.1:1/nope", schema: "remembering_x",
        projectDirectory: dir, httpUrl: "https://memory.example.com", httpTimeoutMs: 2000, jsonPath: ".remembering/store",
      });
      expect(resolved.selection.activeBackend).toBe("http");
      expect(resolved.selection.primaryBackend).toBe("postgres");
      expect(resolved.selection.fallback).toBe(true);
      expect(resolved.selection.fallbackTier).toBe("secondary");
      expect(resolved.selection.primaryReachable).toBe(false);
      expect(resolved.selection.reason).toContain("DB_UNREACHABLE");
      await resolved.close();
    } finally {
      globalThis.fetch = original;
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  test("security failure does not fall back", async () => {
    const original = globalThis.fetch;
    // Mock HTTP backend returning 403: must surface AUTH, never JSON fallback.
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => new Response("forbidden", { status: 403 })) as unknown as typeof fetch;
    const dir = await tempDir();
    try {
      let code = "";
      try {
        await resolveStores({
          mode: "http", dsn: "postgresql://127.0.0.1:1/nope", schema: "remembering_x",
          projectDirectory: dir, httpUrl: "https://memory.example.com", httpTimeoutMs: 2000, jsonPath: ".remembering/store",
        });
      } catch (error) {
        code = (error as { code?: string }).code ?? String(error);
      }
      expect(code).toContain("HTTP_BACKEND_AUTH");
    } finally {
      globalThis.fetch = original;
      await fs.rm(dir, { recursive: true, force: true });
    }
  });
});

describe("HTTP backend contract", () => {
  test("sends bearer token, translates responses, redacts secrets", async () => {
    const seen: Record<string, string> = {};
    const original = globalThis.fetch;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async (url: unknown, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      seen["auth"] = headers.get("authorization") ?? "";
      const u = String(url);
      if (u.endsWith("/rpc/remembering_lexical_search")) {
        return Response.json({ items: [{ chunk_id: "c1", source_id: "s1", text: "hi", section: null, score: 1.2, rank: 1 }] });
      }
      return Response.json({ ok: true });
    }) as unknown as typeof fetch;
    try {
      const store = new HttpBaselineStore({ url: "https://memory.example.com", token: "secret-token-123", timeoutMs: 2000 }, "remembering_x");
      const items = await store.lexicalSearch("hi", 5);
      expect(items[0]?.chunkId).toBe("c1");
      expect(seen["auth"]).toBe("Bearer secret-token-123");
      expect(redactSecrets(`Authorization: Bearer secret-token-123`)).not.toContain("secret-token-123");
    } finally {
      globalThis.fetch = original;
    }
  });

  test("malformed JSON becomes protocol error, unreachable becomes coded error", async () => {
    const original = globalThis.fetch;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => new Response("not-json{", { status: 200 })) as unknown as typeof fetch;
    try {
      const store = new HttpBaselineStore({ url: "https://memory.example.com", timeoutMs: 500 }, "remembering_x");
      await expect(store.stats()).rejects.toThrow("HTTP_BACKEND_PROTOCOL");
    } finally {
      globalThis.fetch = original;
    }
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async () => { throw new Error("boom"); }) as unknown as typeof fetch;
    try {
      const store = new HttpBaselineStore({ url: "https://memory.example.com", timeoutMs: 500 }, "remembering_x");
      await expect(store.stats()).rejects.toThrow("HTTP_BACKEND_UNREACHABLE");
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("embeddings over HTTP", () => {
  test("remote host is used and bearer token sent without leaking", async () => {
    const seen: string[] = [];
    const original = globalThis.fetch;
    (globalThis as unknown as { fetch: typeof fetch }).fetch = (async (url: unknown, init?: RequestInit) => {
      seen.push(String(url));
      const headers = new Headers(init?.headers);
      seen.push(headers.get("authorization") ?? "");
      return Response.json({ embeddings: [[0.1, 0.2]] });
    }) as unknown as typeof fetch;
    try {
      const { OllamaEmbeddingProvider } = await import("../embeddings");
      const provider = new OllamaEmbeddingProvider("bge-m3", "https://embeddings.example.com", { bearerToken: "emb-secret" });
      const out = await provider.embed(["hello"]);
      expect(out.dimension).toBe(2);
      expect(seen[0]).toContain("https://embeddings.example.com/api/embed");
      expect(seen[1]).toBe("Bearer emb-secret");
    } finally {
      globalThis.fetch = original;
    }
  });
});
