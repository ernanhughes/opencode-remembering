import type { Pool, PoolClient } from "pg";

import { ident, toRegclassParam } from "./db";
import { EngineError } from "./errors";

export const SCHEMA_VERSION = "0.1.0";
export const ENGINE_VERSION = "remembering-engine-v0.1";

export type ScoredChunk = {
  chunkId: string;
  sourceId: string;
  text: string;
  section: string | null;
  score: number;
  rank: number;
};

export type ChunkRow = {
  chunkId: string;
  sourceId: string;
  ordinal: number;
  text: string;
  section: string | null;
  charStart: number;
  charEnd: number;
  contentHash: string;
  chunker: string;
  embeddingVersion: string;
  embedding: number[];
};

/** Port of engine/remembering/baseline/storage.py Store. */
export class BaselineStore {
  constructor(
    private readonly pool: Pool,
    readonly dsn: string,
    readonly schema: string,
  ) {}

  async initialise(embeddingDim: number): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query("CREATE EXTENSION IF NOT EXISTS vector");
      await client.query("CREATE EXTENSION IF NOT EXISTS pg_trgm");
      await client.query(`CREATE SCHEMA IF NOT EXISTS ${s}`);
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)`,
      );
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.sources (` +
          `source_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL, content_hash TEXT NOT NULL, ` +
          `timestamp TEXT, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())`,
      );
      // pgvector dimension is interpolated as an integer literal; the
      // schema name is validated and quoted above.
      const dim = Math.floor(embeddingDim);
      if (!Number.isInteger(dim) || dim < 1 || dim > 65535) {
        throw new EngineError("CONFIG_INVALID", `refusing invalid embedding dimension ${JSON.stringify(embeddingDim)}.`);
      }
      await client.query(
        `CREATE TABLE IF NOT EXISTS ${s}.chunks (` +
          `chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES ${s}.sources(source_id) ON DELETE CASCADE, ` +
          `ordinal INT NOT NULL, text TEXT NOT NULL, section TEXT, char_start INT NOT NULL, char_end INT NOT NULL, ` +
          `content_hash TEXT NOT NULL, chunker TEXT NOT NULL, embedding_version TEXT NOT NULL, ` +
          `embedding vector(${dim}) NOT NULL, tsv TSVECTOR NOT NULL)`,
      );
      await client.query(
        `CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON ${s}.chunks USING GIN (tsv)`,
      );
    } finally {
      client.release();
    }
    await this.checkDimension(embeddingDim);
  }

  async checkDimension(embeddingDim: number): Promise<void> {
    const client = await this.pool.connect();
    try {
      const res = await client.query(
        "SELECT atttypmod FROM pg_attribute " +
          "JOIN pg_class ON pg_class.oid = pg_attribute.attrelid " +
          "JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace " +
          "WHERE pg_namespace.nspname = $1 AND pg_class.relname = 'chunks' AND pg_attribute.attname = 'embedding'",
        [this.schema],
      );
      if (res.rowCount && res.rows[0].atttypmod !== -1) {
        const actual = Number(res.rows[0].atttypmod);
        if (actual !== embeddingDim) {
          throw new EngineError(
            "DIMENSION_MISMATCH",
            `embedding dimension mismatch: table holds ${actual}, configuration requests ${embeddingDim}. Re-embed with a fresh schema instead of mixing dimensions.`,
          );
        }
      }
    } finally {
      client.release();
    }
  }

  async upsertSource(sourceId: string, artifactType: string, contentHash: string, timestamp: string | null): Promise<boolean> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const existing = await client.query(`SELECT content_hash FROM ${s}.sources WHERE source_id = $1`, [sourceId]);
      if (existing.rowCount && existing.rows[0].content_hash === contentHash) return false;
      await client.query(
        `INSERT INTO ${s}.sources (source_id, artifact_type, content_hash, timestamp) VALUES ($1,$2,$3,$4) ` +
          `ON CONFLICT (source_id) DO UPDATE SET artifact_type = EXCLUDED.artifact_type, ` +
          `content_hash = EXCLUDED.content_hash, timestamp = EXCLUDED.timestamp, ingested_at = now()`,
        [sourceId, artifactType, contentHash, timestamp],
      );
      return true;
    } finally {
      client.release();
    }
  }

  async knownSources(): Promise<Record<string, string>> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT source_id, content_hash FROM ${s}.sources`);
      const out: Record<string, string> = {};
      for (const row of res.rows) out[row.source_id] = row.content_hash;
      return out;
    } finally {
      client.release();
    }
  }

  async removeSource(sourceId: string): Promise<number> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`DELETE FROM ${s}.sources WHERE source_id = $1`, [sourceId]);
      return res.rowCount ?? 0;
    } finally {
      client.release();
    }
  }

  async replaceChunks(sourceId: string, rows: ChunkRow[]): Promise<void> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`DELETE FROM ${s}.chunks WHERE source_id = $1`, [sourceId]);
      for (const row of rows) {
        const literal = `[${row.embedding.map((v) => Number(v)).join(",")}]`;
        await client.query(
          `INSERT INTO ${s}.chunks (chunk_id, source_id, ordinal, text, section, char_start, char_end, ` +
            `content_hash, chunker, embedding_version, embedding, tsv) ` +
            `VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector, to_tsvector('english', $4))`,
          [row.chunkId, row.sourceId, row.ordinal, row.text, row.section, row.charStart, row.charEnd,
            row.contentHash, row.chunker, row.embeddingVersion, literal],
        );
      }
    } finally {
      client.release();
    }
  }

  async lexicalSearch(query: string, k: number): Promise<ScoredChunk[]> {
    const terms = query.toLowerCase().match(/[a-z0-9]+/g)?.filter((t) => t.length > 1) ?? [];
    if (terms.length === 0) return [];
    const orQuery = terms.join(" | ");
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(
        `SELECT chunk_id, source_id, text, section, ts_rank_cd(tsv, to_tsquery('english', $1)) ` +
          `FROM ${s}.chunks WHERE tsv @@ to_tsquery('english', $1) ORDER BY 5 DESC, chunk_id LIMIT $2`,
        [orQuery, k],
      );
      return res.rows.map((row, i) => ({
        chunkId: row.chunk_id, sourceId: row.source_id, text: row.text,
        section: row.section ?? null, score: Number(row.ts_rank_cd ?? row["?column?"] ?? 0), rank: i + 1,
      }));
    } finally {
      client.release();
    }
  }

  async denseSearch(vector: number[], k: number, hnswProbe = 16): Promise<ScoredChunk[]> {
    const literal = `[${vector.map((v) => Number(v)).join(",")}]`;
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(`SET LOCAL hnsw.ef_search = ${Math.floor(hnswProbe)}`).catch(() => {});
      const res = await client.query(
        `SELECT chunk_id, source_id, text, section, 1 - (embedding <=> $1::vector) AS score ` +
          `FROM ${s}.chunks ORDER BY embedding <=> $1::vector, chunk_id LIMIT $2`,
        [literal, k],
      );
      return res.rows.map((row, i) => ({
        chunkId: row.chunk_id, sourceId: row.source_id, text: row.text,
        section: row.section ?? null, score: Number(row.score), rank: i + 1,
      }));
    } finally {
      client.release();
    }
  }

  async ensureHnsw(): Promise<boolean> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      await client.query(
        `CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON ${s}.chunks USING hnsw (embedding vector_cosine_ops)`,
      );
      return true;
    } catch (error) {
      // 54000 / program_limit_exceeded: vector too wide for HNSW.
      if ((error as { code?: string })?.code === "54000") return false;
      throw error;
    } finally {
      client.release();
    }
  }

  async stats(): Promise<Record<string, unknown>> {    const s = ident(this.schema);
    const out: Record<string, unknown> = {};
    const client = await this.pool.connect();
    try {
      for (const [label, query] of [
        ["sources", `SELECT count(*) FROM ${s}.sources`],
        ["chunks", `SELECT count(*) FROM ${s}.chunks`],
        ["duplicate_chunks", `SELECT count(*) - count(DISTINCT content_hash) FROM ${s}.chunks`],
        ["missing_vectors", `SELECT count(*) FROM ${s}.chunks WHERE embedding IS NULL`],
        ["distinct_embedding_versions", `SELECT count(DISTINCT embedding_version) FROM ${s}.chunks`],
        ["distinct_chunkers", `SELECT count(DISTINCT chunker) FROM ${s}.chunks`],
      ] as Array<[string, string]>) {
        const qr = await client.query(query);
        const row0 = qr.rows[0] as Record<string, unknown> | undefined;
        out[label] = Number((row0?.["count"] ?? row0?.["?column?"] ?? 0) as unknown as string);
      }
      const versions = await client.query(
        `SELECT embedding_version, count(*) FROM ${s}.chunks GROUP BY 1 ORDER BY 2 DESC`,
      );
      out["embedding_versions"] = versions.rows.map((r) => ({ version: r.embedding_version, chunks: Number(r.count) }));
      const idx = await client.query("SELECT to_regclass($1), to_regclass($2)", [
        toRegclassParam(this.schema, "chunks_tsv_idx"),
        toRegclassParam(this.schema, "chunks_embedding_hnsw"),
      ]);
      // pg returns columns named to_regclass; second identical name is renamed.
      const firstRow = idx.rows[0] as Record<string, unknown> | undefined;
      const keys = Object.keys(firstRow ?? {});
      const firstKey = keys[0] as string | undefined;
      const secondKey = keys[1] as string | undefined;
      out["fts_index"] = firstKey && firstRow ? (firstRow[firstKey] ?? null) : null;
      out["hnsw_index"] = secondKey && firstRow ? (firstRow[secondKey] ?? null) : null;
      return out;
    } finally {
      client.release();
    }
  }

  async orphanChunks(): Promise<number> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(
        `SELECT count(*) FROM ${s}.chunks c LEFT JOIN ${s}.sources s ON c.source_id = s.source_id WHERE s.source_id IS NULL`,
      );
      return Number((res.rows[0] as Record<string, unknown>)?.["count"] ?? 0);
    } finally {
      client.release();
    }
  }

  async embeddingVersions(): Promise<string[]> {
    const s = ident(this.schema);
    const client = await this.pool.connect();
    try {
      const res = await client.query(`SELECT DISTINCT embedding_version FROM ${s}.chunks`);
      return res.rows.map((r) => String(r.embedding_version));
    } finally {
      client.release();
    }
  }
}

export async function readProjectMeta(client: PoolClient, schema: string): Promise<Record<string, string>> {
  const s = ident(schema);
  try {
    const res = await client.query(`SELECT key, value FROM ${s}.meta`);
    const out: Record<string, string> = {};
    for (const row of res.rows) out[row.key] = row.value;
    return out;
  } catch {
    return {};
  }
}
