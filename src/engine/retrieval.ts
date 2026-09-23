import type { EmbeddingProvider } from "./embeddings";
import type { ScoredChunk } from "./storage";

export type RetrievalMode = "lexical" | "dense" | "hybrid";
export type RerankerKind = "none" | "overlap" | "cross-encoder";

export type RetrievalConfig = {
  mode: RetrievalMode;
  lexicalK: number;
  denseK: number;
  fusionK: number;
  rerankK: number;
  reranker: RerankerKind;
  rerankerModel?: string;
};

export type RetrievalTrace = {
  query: string;
  lexical: ScoredChunk[];
  dense: ScoredChunk[];
  fused: ScoredChunk[];
  reranked: ScoredChunk[];
  latenciesMs: Record<string, number>;
};

export type StoreRetrievalPort = {
  lexicalSearch(query: string, k: number): Promise<ScoredChunk[]>;
  denseSearch(vector: number[], k: number): Promise<ScoredChunk[]>;
};

/** RRF: parameter-free fusion over ranks. Port of retrieval.reciprocal_rank_fusion. */
export function reciprocalRankFusion(rankings: ScoredChunk[][], k = 60): ScoredChunk[] {
  const scores = new Map<string, number>();
  const byId = new Map<string, ScoredChunk>();
  for (const ranking of rankings) {
    ranking.forEach((chunk, index) => {
      const rank = index + 1;
      scores.set(chunk.chunkId, (scores.get(chunk.chunkId) ?? 0) + 1 / (k + rank));
      if (!byId.has(chunk.chunkId)) byId.set(chunk.chunkId, chunk);
    });
  }
  const ordered = [...scores.keys()].sort((a, b) => {
    const diff = (scores.get(b) ?? 0) - (scores.get(a) ?? 0);
    if (diff !== 0) return diff;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  return ordered.map((cid, i) => {
    const origin = byId.get(cid);
    if (!origin) throw new Error("RRF invariant violated: missing chunk");
    return {
      chunkId: cid,
      sourceId: origin.sourceId,
      text: origin.text,
      section: origin.section,
      score: scores.get(cid) ?? 0,
      rank: i + 1,
    };
  });
}

/** Transparent test-double reranker: token overlap with the query. */
export function overlapRerank(query: string, chunks: ScoredChunk[], k: number): ScoredChunk[] {
  const queryTokens = new Set(query.toLowerCase().split(/\s+/).filter(Boolean));
  const scored = chunks.map((chunk) => {
    const chunkTokens = new Set(chunk.text.toLowerCase().split(/\s+/).filter(Boolean));
    let overlap = 0;
    for (const token of queryTokens) if (chunkTokens.has(token)) overlap += 1;
    return { overlap, chunk };
  });
  scored.sort((a, b) => {
    if (b.overlap !== a.overlap) return b.overlap - a.overlap;
    return a.chunk.chunkId < b.chunk.chunkId ? -1 : a.chunk.chunkId > b.chunk.chunkId ? 1 : 0;
  });
  return scored.slice(0, k).map(({ overlap, chunk }, i) => ({
    chunkId: chunk.chunkId,
    sourceId: chunk.sourceId,
    text: chunk.text,
    section: chunk.section,
    score: overlap,
    rank: i + 1,
  }));
}

export class Retriever {
  constructor(
    private readonly store: StoreRetrievalPort,
    private readonly embedder: EmbeddingProvider,
    private readonly cfg: RetrievalConfig,
  ) {
    if (cfg.reranker === "cross-encoder") {
      throw new Error(
        'reranker "cross-encoder" requires sentence-transformers (Python) and is refused by the TypeScript engine; use "none" or "overlap".',
      );
    }
  }

  async retrieve(query: string): Promise<RetrievalTrace> {
    const trace: RetrievalTrace = {
      query,
      lexical: [],
      dense: [],
      fused: [],
      reranked: [],
      latenciesMs: {},
    };
    if (this.cfg.mode === "lexical" || this.cfg.mode === "hybrid") {
      const started = performance.now();
      trace.lexical = await this.store.lexicalSearch(query, this.cfg.lexicalK);
      trace.latenciesMs["lexical"] = performance.now() - started;
    }
    if (this.cfg.mode === "dense" || this.cfg.mode === "hybrid") {
      const started = performance.now();
      const vector = (await this.embedder.embed([query])).vectors[0];
      if (!vector) throw new Error("embedder returned no vector");
      trace.dense = await this.store.denseSearch(vector, this.cfg.denseK);
      trace.latenciesMs["dense"] = performance.now() - started;
    }
    if (this.cfg.mode === "lexical") trace.fused = trace.lexical;
    else if (this.cfg.mode === "dense") trace.fused = trace.dense;
    else trace.fused = reciprocalRankFusion([trace.lexical, trace.dense], this.cfg.fusionK);

    if (this.cfg.reranker === "overlap") {
      const started = performance.now();
      trace.reranked = overlapRerank(query, trace.fused, this.cfg.rerankK);
      trace.latenciesMs["rerank"] = performance.now() - started;
    } else {
      trace.reranked = trace.fused.slice(0, this.cfg.rerankK);
    }
    return trace;
  }
}
