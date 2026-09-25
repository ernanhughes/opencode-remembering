import { cosine } from "../../embeddings";
import type { ChunkRow, ScoredChunk } from "../../storage";

/** BM25-like lexical scorer: real ranking, not substring matching. */

type DocStats = { tf: Map<string, number>; length: number };

export function tokenize(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9]+/g)?.filter((t) => t.length > 1) ?? [];
}

export function bm25Scores(
  query: string,
  docs: Array<{ chunkId: string; text: string }>,
  k1 = 1.2,
  b = 0.75,
): Map<string, number> {
  const N = docs.length;
  if (N === 0) return new Map();
  const docTokens = docs.map((d) => ({ id: d.chunkId, tokens: tokenize(d.text) }));
  const df = new Map<string, number>();
  const perDoc: DocStats[] = docTokens.map(({ tokens }) => {
    const tf = new Map<string, number>();
    for (const t of tokens) tf.set(t, (tf.get(t) ?? 0) + 1);
    for (const t of tf.keys()) df.set(t, (df.get(t) ?? 0) + 1);
    return { tf, length: tokens.length };
  });
  const avgLen = perDoc.reduce((n, d) => n + d.length, 0) / Math.max(1, perDoc.length);
  const qTerms = tokenize(query);
  const scores = new Map<string, number>();
  docTokens.forEach((doc, i) => {
    const stats = perDoc[i]!;
    let score = 0;
    for (const term of new Set(qTerms)) {
      const tf = stats.tf.get(term) ?? 0;
      if (!tf) continue;
      const docFreq = df.get(term) ?? 1;
      const idf = Math.log(1 + (N - docFreq + 0.5) / (docFreq + 0.5));
      const denom = tf + k1 * (1 - b + (b * stats.length) / Math.max(1, avgLen));
      score += idf * ((tf * (k1 + 1)) / denom);
    }
    if (score > 0) scores.set(doc.id, score);
  });
  return scores;
}

export function lexicalRank(query: string, rows: ChunkRow[], k: number): ScoredChunk[] {
  const scores = bm25Scores(query, rows.map((r) => ({ chunkId: r.chunkId, text: r.text })));
  const byId = new Map(rows.map((r) => [r.chunkId, r]));
  return [...scores.entries()]
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .slice(0, k)
    .map(([chunkId, score], i) => {
      const row = byId.get(chunkId)!;
      return { chunkId, sourceId: row.sourceId, text: row.text, section: row.section, score, rank: i + 1 };
    });
}

/** Linear cosine scan over stored embeddings. Honest: not HNSW/pgvector. */
export function denseRank(queryVector: number[], rows: ChunkRow[], k: number): ScoredChunk[] {
  const scored = rows
    .filter((r) => Array.isArray(r.embedding) && r.embedding.length > 0)
    .map((r) => ({ row: r, score: cosine(queryVector, r.embedding) }))
    .filter((s) => Number.isFinite(s.score));
  scored.sort((a, b) => b.score - a.score || (a.row.chunkId < b.row.chunkId ? -1 : 1));
  return scored.slice(0, k).map((s, i) => ({
    chunkId: s.row.chunkId, sourceId: s.row.sourceId, text: s.row.text,
    section: s.row.section, score: s.score, rank: i + 1,
  }));
}
