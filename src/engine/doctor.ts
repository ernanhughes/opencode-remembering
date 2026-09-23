import { realpath } from "node:fs/promises";
import * as path from "node:path";
import type { Pool } from "pg";

import { checkOllamaModel, buildEmbedder, type EmbeddingSpec } from "./embeddings";
import { EngineError, redactDsn } from "./errors";
import { extensionStatus, withClient } from "./db";
import { computeBaselineReadiness } from "./readiness";
import { ENGINE_VERSION, readProjectMeta } from "./storage";

export type BaselineDoctorOptions = {
  dsn: string;
  schema: string;
  projectDirectory: string;
  embedding: EmbeddingSpec;
};

/** Canonical project identity: realpath + lowercase-on-Windows. */
export async function canonicalProjectDir(directory: string): Promise<string> {
  let resolved: string;
  try {
    resolved = await realpath(directory);
  } catch {
    resolved = path.resolve(directory);
  }
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

/**
 * Stage 1 native doctor: baseline PostgreSQL + pgvector/pg_trgm +
 * schema identity + embedding reachability. Temporal/frame/trust/
 * trace/loops/write sections are returned as not-ready stubs until
 * their TypeScript ports land (Stages 2+).
 */
export async function doctorBaseline(pool: Pool, options: BaselineDoctorOptions) {
  const { dsn, schema, projectDirectory, embedding } = options;
  const canonicalDir = await canonicalProjectDir(projectDirectory);
  const redacted = redactDsn(dsn);
  const report: Record<string, unknown> = {
    ok: false,
    engine_root: "typescript:src/engine",
    engine_version: ENGINE_VERSION,
    engine_available: true,
    dsn_redacted: redacted,
    schema,
    project_directory: canonicalDir,
    postgres_reachable: false,
    database_exists: false,
    pgvector_available: false,
    pgvector_version: null,
    pg_trgm_available: false,
    schema_initialized: false,
    schema_identity_ok: true,
    embedding_provider_reachable: false,
    embedding_model_available: false,
    embedding_detail: "",
    stored_embedding_version: null,
    stored_embedding_dimension: null,
    configured_embedding_version: `${embedding.provider}:${embedding.model}`,
    embedding_dimension_compatible: null,
    fts_index_present: false,
    hnsw_index_present: false,
    source_count: null,
    chunk_count: null,
    indexed: false,
    chunks: null,
  };

  await withClient(pool, async (client) => {
    report["postgres_reachable"] = true;
    report["database_exists"] = true;
    const vector = await extensionStatus(client, "vector");
    const trgm = await extensionStatus(client, "pg_trgm");
    report["pgvector_available"] = vector.installed;
    report["pgvector_version"] = vector.version ?? "unknown";
    report["pg_trgm_available"] = trgm.installed;
    const reg = await client.query("SELECT to_regclass($1)", [`${schema}.chunks`]);
    const initialized = reg.rows[0]?.to_regclass != null;
    report["schema_initialized"] = initialized;
    report["indexed"] = initialized;
    if (!initialized) return;
    const meta = await readProjectMeta(client, schema);
    const recorded = meta["project.path"];
    if (recorded != null && recorded !== canonicalDir) {
      report["schema_identity_ok"] = false;
      throw new EngineError(
        "SCHEMA_MISMATCH",
        `schema ${JSON.stringify(schema)} is claimed by project ${JSON.stringify(recorded)}, not ${JSON.stringify(canonicalDir)}. Refusing to read another project's memory.`,
      );
    }
    const sources = await client.query(`SELECT count(*) FROM "${schema}".sources`);
    const chunks = await client.query(`SELECT count(*) FROM "${schema}".chunks`);
    report["source_count"] = Number(sources.rows[0].count);
    report["chunk_count"] = Number(chunks.rows[0].count);
    report["chunks"] = report["chunk_count"];
    const versions = await client.query(
      `SELECT embedding_version, count(*) FROM "${schema}".chunks GROUP BY 1 ORDER BY 2 DESC LIMIT 1`,
    );
    if (versions.rowCount) report["stored_embedding_version"] = String(versions.rows[0].embedding_version);
    const dim = await client.query(
      "SELECT atttypmod FROM pg_attribute JOIN pg_class ON pg_class.oid = pg_attribute.attrelid " +
        "JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace " +
        "WHERE pg_namespace.nspname = $1 AND pg_class.relname = 'chunks' AND pg_attribute.attname = 'embedding'",
      [schema],
    );
    if (dim.rowCount && dim.rows[0].atttypmod !== -1) {
      report["stored_embedding_dimension"] = Number(dim.rows[0].atttypmod);
    }
    const idx = await client.query("SELECT to_regclass($1) AS fts, to_regclass($2) AS hnsw", [
      `${schema}.chunks_tsv_idx`,
      `${schema}.chunks_embedding_hnsw`,
    ]);
    report["fts_index_present"] = idx.rows[0]?.fts != null;
    report["hnsw_index_present"] = idx.rows[0]?.hnsw != null;
  });

  // Embedding reachability (never downloads or substitutes models).
  if (embedding.provider === "ollama") {
    const check = await checkOllamaModel(embedding.host, embedding.model);
    report["embedding_provider_reachable"] = true;
    report["embedding_model_available"] = true;
    report["embedding_detail"] = check.detail;
    if (typeof report["stored_embedding_dimension"] === "number") {
      const embedder = buildEmbedder(embedding);
      const probe = await embedder.embed(["dimension probe"]);
      report["embedding_dimension_compatible"] = probe.dimension === report["stored_embedding_dimension"];
      if (!report["embedding_dimension_compatible"]) {
        throw new EngineError(
          "DIMENSION_MISMATCH",
          `store holds ${report["stored_embedding_dimension"]}-dimensional vectors but ${embedding.provider}:${embedding.model} produces ${probe.dimension}. Re-embed with a fresh schema instead of mixing dimensions.`,
        );
      }
    }
  } else if (embedding.provider === "hashing") {
    report["embedding_provider_reachable"] = true;
    report["embedding_model_available"] = true;
    report["embedding_detail"] = "hashing test double (REMEMBERING_ALLOW_TEST_EMBEDDINGS)";
  } else {
    throw new EngineError(
      "CONFIG_INVALID",
      `embedding provider ${JSON.stringify(embedding.provider)} is not supported by the TypeScript engine; use "ollama".`,
    );
  }

  // Explicit readiness calculation: every field above was probed, so derive
  // ok from actual state instead of leaving the initial false in place.
  const readiness = computeBaselineReadiness({
    postgresReachable: report["postgres_reachable"] === true,
    databaseExists: report["database_exists"] === true,
    pgvectorAvailable: report["pgvector_available"] === true,
    pgTrgmAvailable: report["pg_trgm_available"] === true,
    schemaInitialized: report["schema_initialized"] === true,
    schemaIdentityOk: report["schema_identity_ok"] !== false,
    embeddingProviderReachable: report["embedding_provider_reachable"] === true,
    embeddingModelAvailable: report["embedding_model_available"] === true,
    embeddingDimensionCompatible:
      typeof report["embedding_dimension_compatible"] === "boolean"
        ? (report["embedding_dimension_compatible"] as boolean)
        : null,
  });
  report["ok"] = readiness.ok;
  report["readiness"] = { code: readiness.code, reasons: readiness.reasons };
  if (readiness.ok) {
    report["message"] =
      `Remembering engine ${ENGINE_VERSION} is ready: schema ${schema} holds ` +
      `${(report["chunk_count"] as number | null) ?? 0} chunks from ${(report["source_count"] as number | null) ?? 0} sources.`;
  } else {
    report["message"] = `${readiness.code}: ${readiness.reasons.join("; ")}`;
  }

  return report;
}
