import { describe, expect, test } from "bun:test";

import { HashingEmbedder } from "./embeddings";
import { overlapRerank, reciprocalRankFusion, Retriever } from "./retrieval";
import type { ScoredChunk } from "./storage";

process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS = "1";

function chunk(id: string, rank: number, score = 1): ScoredChunk {
  return { chunkId: id, sourceId: `src-${id}`, text: `text ${id}`, section: null, score, rank };
}

describe("reciprocal_rank_fusion parity (baseline/retrieval.py)", () => {
  test("single ranking preserves order with RRF scores", () => {
    const fused = reciprocalRankFusion([[chunk("b", 1), chunk("a", 2)]], 60);
    expect(fused.map((c) => c.chunkId)).toEqual(["b", "a"]);
    expect(fused[0]?.score).toBeCloseTo(1 / 61, 9);
    expect(fused.map((c) => c.rank)).toEqual([1, 2]);
  });

  test("shared hits outrank single-list hits; ties break by chunk_id", () => {
    const lexical = [chunk("a", 1), chunk("b", 2)];
    const dense = [chunk("b", 1), chunk("c", 2)];
    const fused = reciprocalRankFusion([lexical, dense], 60);
    expect(fused.map((c) => c.chunkId)).toEqual(["b", "a", "c"]);
  });

  test("fusion k changes absolute scores but not this ordering", () => {
    const lexical = [chunk("a", 1)];
    const dense = [chunk("a", 1)];
    const small = reciprocalRankFusion([lexical, dense], 1);
    const large = reciprocalRankFusion([lexical, dense], 60);
    expect(small[0]?.score).toBeGreaterThan(large[0]?.score ?? 0);
  });
});

describe("overlap reranker parity", () => {
  test("orders by token overlap then chunk_id", () => {
    const chunks = [chunk("c2", 1), chunk("c1", 2), chunk("c3", 3)];
    chunks[0]!.text = "unrelated words here";
    chunks[1]!.text = "authentication design changed";
    chunks[2]!.text = "authentication design changed twice";
    const ranked = overlapRerank("authentication design", chunks, 3);
    expect(ranked.map((c) => c.chunkId)).toEqual(["c1", "c3", "c2"]);
    expect(ranked[0]?.rank).toBe(1);
  });

  test("respects k", () => {
    const chunks = [chunk("a", 1), chunk("b", 2)];
    expect(overlapRerank("a b", chunks, 1)).toHaveLength(1);
  });
});

describe("Retriever stage semantics", () => {
  test("lexical mode never calls the embedder", async () => {
    let calls = 0;
    const store = {
      lexicalSearch: async () => [chunk("a", 1)],
      denseSearch: async () => { throw new Error("must not run"); },
    };
    const embedder = new HashingEmbedder(8);
    const counting = {
      name: "hashing",
      version: () => "hashing:8",
      embed: async (texts: string[]) => { calls += 1; return embedder.embed(texts); },
    };
    const retriever = new Retriever(store, counting, {
      mode: "lexical", lexicalK: 5, denseK: 5, fusionK: 60, rerankK: 5, reranker: "none",
    });
    const trace = await retriever.retrieve("hello");
    expect(trace.lexical.map((c) => c.chunkId)).toEqual(["a"]);
    expect(trace.dense).toEqual([]);
    expect(trace.fused.map((c) => c.chunkId)).toEqual(["a"]);
    expect(calls).toBe(0);
    expect(trace.latenciesMs["dense"]).toBeUndefined();
  });

  test("hybrid fuses both stages and records latencies", async () => {
    const store = {
      lexicalSearch: async () => [chunk("a", 1), chunk("b", 2)],
      denseSearch: async () => [chunk("b", 1), chunk("c", 2)],
    };
    const retriever = new Retriever(store, new HashingEmbedder(8), {
      mode: "hybrid", lexicalK: 5, denseK: 5, fusionK: 60, rerankK: 5, reranker: "none",
    });
    const trace = await retriever.retrieve("query");
    expect(trace.fused.map((c) => c.chunkId)).toEqual(["b", "a", "c"]);
    expect(trace.reranked.map((c) => c.chunkId)).toEqual(["b", "a", "c"]);
    expect(trace.latenciesMs["lexical"]).toBeDefined();
    expect(trace.latenciesMs["dense"]).toBeDefined();
  });

  test("cross-encoder reranker is refused by the TS engine", () => {
    const store = { lexicalSearch: async () => [], denseSearch: async () => [] };
    expect(
      () => new Retriever(store, new HashingEmbedder(8), {
        mode: "hybrid", lexicalK: 5, denseK: 5, fusionK: 60, rerankK: 5, reranker: "cross-encoder",
      }),
    ).toThrow(/cross-encoder/);
  });
});
