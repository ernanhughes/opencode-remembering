import { describe, expect, test } from "bun:test";

import { HashingEmbedder, cosine } from "./embeddings";
import { chunkFixed, chunkSection, chunkSentenceAware, chunkSource, sha1, type Source } from "./ingest";

process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS = "1";

describe("hashing embedder parity (baseline/embeddings.py)", () => {
  test("token-hash buckets are deterministic and L2-normalized", async () => {
    const embedder = new HashingEmbedder(64);
    const first = await embedder.embed(["hello world"]);
    const second = await embedder.embed(["hello world"]);
    const firstVec = first.vectors[0];
    const secondVec = second.vectors[0];
    expect(firstVec).toEqual(secondVec);
    if (!firstVec) throw new Error("missing vector");
    const norm = Math.sqrt(firstVec.reduce((acc, v) => acc + v * v, 0));
    expect(norm).toBeCloseTo(1, 6);
    expect(first.dimension).toBe(64);
    expect(first.provider).toBe("hashing");
  });

  test("different tokens land in different buckets", async () => {
    const embedder = new HashingEmbedder(64);
    const res = await embedder.embed(["alpha", "beta"]);
    expect(res.vectors[0]).not.toEqual(res.vectors[1]);
  });
});

describe("ingest parity (baseline/ingest.py)", () => {
  test("sha1 matches Python hashlib.sha1", () => {
    // echo -n "hello" | sha1sum -> aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d
    expect(sha1("hello")).toBe("aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d");
  });

  test("fixed chunking overlaps like the Python port", () => {
    const text = "abcdefghij";
    expect(chunkFixed(text, 4, 1)).toEqual([[0, 4], [3, 7], [6, 10]]);
  });

  test("section chunking keeps headings", () => {
    const text = "intro\n# Hello\nbody\n## Sub\nmore";
    const spans = chunkSection(text);
    expect(spans.length).toBe(3);
    expect(spans[1]?.[2]).toBe("# Hello");
    expect(spans[2]?.[2]).toBe("## Sub");
  });

  test("sentence chunking is deterministic", () => {
    const text = "First sentence. Second sentence. Third sentence here.";
    const spans = chunkSentenceAware(text, 30, 5);
    expect(spans.length).toBeGreaterThan(0);
    const first = spans[0];
    if (!first) throw new Error("missing span");
    const [start, end] = first;
    expect(text.slice(start, end)).toContain("First sentence.");
  });

  test("chunk ids match the Python scheme (16 hex chars, deterministic)", () => {
    const source: Source = {
      sourceId: "docs/a.md",
      artifactType: "document",
      content: "Hello world. Second sentence here.",
      contentHash: sha1("Hello world. Second sentence here."),
      timestamp: null,
    };
    const first = chunkSource(source, { policy: "sentence", targetChars: 2000, overlapChars: 300, chunkerVersion: "0.1.0" });
    const second = chunkSource(source, { policy: "sentence", targetChars: 2000, overlapChars: 300, chunkerVersion: "0.1.0" });
    expect(first).toEqual(second);
    for (const chunk of first) {
      expect(chunk.chunkId).toMatch(/^[0-9a-f]{16}$/);
    }
  });

  test("cosine self-similarity is 1", () => {
    expect(cosine([1, 0], [1, 0])).toBeCloseTo(1, 9);
  });
});
