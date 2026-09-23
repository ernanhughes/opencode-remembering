import * as crypto from "node:crypto";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

export type EmbeddingConfig = {
  provider: "ollama" | "sentence-transformers" | "hashing";
  model: string;
  host: string;
};

export type RetrievalConfig = {
  mode: "lexical" | "dense" | "hybrid";
  lexicalK: number;
  denseK: number;
  fusionK: number;
  rerankK: number;
  reranker: "none" | "overlap" | "cross-encoder";
};

export type RememberingConfig = {
  dsn: string;
  schema: string;
  embedding: EmbeddingConfig;
  retrieval: RetrievalConfig;
  context: {
    autoInject: boolean;
    maxChars: number;
    maxResults: number;
  };
};

type RawConfig = {
  dsn?: unknown;
  schema?: unknown;
  // Removed in Stage 3.5 (bundled engine). Still detected so stale
  // configs fail closed; see rejectLegacyRoot.
  project_memory_root?: unknown;
  embedding?: {
    provider?: unknown;
    model?: unknown;
    host?: unknown;
  };
  retrieval?: {
    mode?: unknown;
    lexical_k?: unknown;
    dense_k?: unknown;
    fusion_k?: unknown;
    rerank_k?: unknown;
    reranker?: unknown;
  };
  context?: {
    auto_inject?: unknown;
    max_chars?: unknown;
    max_results?: unknown;
  };
};

const SCHEMA_PATTERN = /^[A-Za-z_][A-Za-z0-9_$]*$/;
export const MAX_SCHEMA_LENGTH = 63;

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function boundedInteger(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
): number {
  if (typeof value === "number" && Number.isInteger(value)) {
    return Math.min(max, Math.max(min, value));
  }
  return fallback;
}

function strictInteger(
  value: unknown,
  field: string,
  min: number,
  max: number,
): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error(
      `Invalid OpenCode Remembering config: ${field} must be an integer`,
    );
  }
  if (value < min || value > max) {
    throw new Error(
      `Invalid OpenCode Remembering config: ${field} must be between ${min} and ${max}`,
    );
  }
  return value;
}

/** Fail closed: an explicit schema override must be a safe SQL identifier. */
export function validateSchemaName(schema: string): string {
  if (!SCHEMA_PATTERN.test(schema) || schema.length > MAX_SCHEMA_LENGTH) {
    throw new Error(
      "Invalid OpenCode Remembering config: schema must be a PostgreSQL " +
        `identifier (1-${MAX_SCHEMA_LENGTH} chars, letters/digits/_/$). ` +
        "Refusing to substitute a global schema.",
    );
  }
  if (schema.toLowerCase().startsWith("pg_")) {
    throw new Error(
      "Invalid OpenCode Remembering config: schema must not use the " +
        "reserved pg_ prefix.",
    );
  }
  return schema;
}

async function canonicalProjectPath(directory: string): Promise<string> {
  try {
    return await fs.realpath(directory);
  } catch {
    return path.resolve(directory);
  }
}

export async function schemaForProject(directory: string): Promise<string> {
  const canonical = await canonicalProjectPath(directory);
  const normalized =
    process.platform === "win32" ? canonical.toLowerCase() : canonical;
  const digest = crypto
    .createHash("sha256")
    .update(normalized)
    .digest("hex")
    .slice(0, 12);
  return `remembering_${digest}`;
}

async function readRawConfig(): Promise<{ raw: RawConfig; path: string }> {
  const configPath = path.join(
    os.homedir(),
    ".config",
    "opencode",
    "remembering.json",
  );
  try {
    const parsed = JSON.parse(await fs.readFile(configPath, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("configuration root must be an object");
    }
    return { raw: parsed as RawConfig, path: configPath };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { raw: {}, path: configPath };
    }
    throw new Error(
      `Invalid OpenCode Remembering config at ${configPath}: ${String(error)}`,
    );
  }
}

function parseEmbedding(raw: RawConfig): EmbeddingConfig {
  const provider =
    nonEmptyString(process.env.REMEMBERING_EMBEDDING_PROVIDER) ??
    nonEmptyString(raw.embedding?.provider) ??
    "ollama";
  if (provider === "hashing") {
    // The hashing embedder is a deterministic test double, never a
    // production retrieval model. It is only honoured for explicitly
    // opted-in local tests, never by silent fallback.
    if (process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS !== "1") {
      throw new Error(
        "Invalid OpenCode Remembering config: embedding.provider " +
          '"hashing" is a test double and is refused without ' +
          "REMEMBERING_ALLOW_TEST_EMBEDDINGS=1.",
      );
    }
  } else if (provider !== "ollama" && provider !== "sentence-transformers") {
    throw new Error(
      `Invalid OpenCode Remembering config: embedding.provider must be ` +
        `"ollama", "sentence-transformers" or "hashing", got ${JSON.stringify(provider)}.`,
    );
  }
  const model =
    nonEmptyString(process.env.REMEMBERING_EMBEDDING_MODEL) ??
    nonEmptyString(raw.embedding?.model) ??
    "bge-m3";
  const host =
    nonEmptyString(process.env.REMEMBERING_EMBEDDING_HOST) ??
    nonEmptyString(raw.embedding?.host) ??
    "http://localhost:11434";
  return { provider, model, host };
}

function parseRetrieval(raw: RawConfig): RetrievalConfig {
  const mode = nonEmptyString(raw.retrieval?.mode) ?? "hybrid";
  if (mode !== "lexical" && mode !== "dense" && mode !== "hybrid") {
    throw new Error(
      `Invalid OpenCode Remembering config: retrieval.mode must be ` +
        `"lexical", "dense" or "hybrid", got ${JSON.stringify(mode)}.`,
    );
  }
  const reranker = nonEmptyString(raw.retrieval?.reranker) ?? "none";
  if (reranker !== "none" && reranker !== "overlap" && reranker !== "cross-encoder") {
    throw new Error(
      `Invalid OpenCode Remembering config: retrieval.reranker must be ` +
        `"none", "overlap" or "cross-encoder", got ${JSON.stringify(reranker)}.`,
    );
  }
  // Default reranker is "none": the engine must not download a
  // cross-encoder model unless the operator explicitly asks for it.
  return {
    mode,
    lexicalK:
      raw.retrieval?.lexical_k === undefined
        ? 30
        : strictInteger(raw.retrieval.lexical_k, "retrieval.lexical_k", 1, 100),
    denseK:
      raw.retrieval?.dense_k === undefined
        ? 30
        : strictInteger(raw.retrieval.dense_k, "retrieval.dense_k", 1, 100),
    fusionK:
      raw.retrieval?.fusion_k === undefined
        ? 60
        : strictInteger(raw.retrieval.fusion_k, "retrieval.fusion_k", 1, 1000),
    rerankK:
      raw.retrieval?.rerank_k === undefined
        ? 8
        : strictInteger(raw.retrieval.rerank_k, "retrieval.rerank_k", 1, 50),
    reranker,
  };
}

export async function loadConfig(
  directory: string,
): Promise<RememberingConfig> {
  const { raw } = await readRawConfig();
  rejectLegacyRoot(raw);

  const dsn =
    nonEmptyString(process.env.MEMORY_BASELINE_DSN) ??
    nonEmptyString(raw.dsn) ??
    "postgresql://postgres:postgres@localhost:5434/memory";

  const rawSchema = nonEmptyString(raw.schema);
  const schema =
    rawSchema !== undefined
      ? validateSchemaName(rawSchema)
      : await schemaForProject(directory);
  const autoInject =
    typeof raw.context?.auto_inject === "boolean"
      ? raw.context.auto_inject
      : true;

  return {
    dsn,
    schema,
    embedding: parseEmbedding(raw),
    retrieval: parseRetrieval(raw),
    context: {
      autoInject,
      maxChars: boundedInteger(raw.context?.max_chars, 4000, 256, 20000),
      maxResults: boundedInteger(raw.context?.max_results, 6, 1, 20),
    },
  };
}

/**
 * The legacy runtime is gone: the memory engine is native TypeScript
 * (src/engine). A stale project_memory_root setting fails closed with
 * migration guidance instead of being silently ignored.
 */
function rejectLegacyRoot(raw: RawConfig): void {
  if (
    nonEmptyString(process.env.PROJECT_MEMORY_ROOT) !== undefined ||
    nonEmptyString(raw.project_memory_root) !== undefined
  ) {
    throw new Error(
      "Invalid OpenCode Remembering config: project_memory_root / " +
        "PROJECT_MEMORY_ROOT no longer exists. The memory engine is " +
        "native TypeScript (src/engine); remove " +
        "the setting — no replacement path is needed.",
    );
  }
}
