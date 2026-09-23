import { createHash } from "node:crypto";

import { EngineError } from "./errors";

export type EmbeddingResult = {
  vectors: number[][];
  provider: string;
  model: string;
  dimension: number;
};

export interface EmbeddingProvider {
  readonly name: string;
  embed(texts: string[]): Promise<EmbeddingResult>;
  version(): string;
}

async function postJson(url: string, payload: unknown, timeoutMs = 300_000): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok) {
      throw new EngineError(
        "EMBEDDING_UNREACHABLE",
        `embedding provider returned HTTP ${res.status} for ${url}`,
      );
    }
    return (await res.json()) as unknown;
  } catch (error) {
    if (error instanceof EngineError) throw error;
    throw new EngineError(
      "EMBEDDING_UNREACHABLE",
      `embedding request failed: ${error instanceof Error ? `${error.name}: ${error.message.slice(0, 160)}` : String(error).slice(0, 160)}`,
    );
  } finally {
    clearTimeout(timer);
  }
}

export class OllamaEmbeddingProvider implements EmbeddingProvider {
  readonly name = "ollama";
  private dimension: number | null = null;
  constructor(
    private readonly model: string,
    private readonly host = "http://localhost:11434",
  ) {}

  async embed(texts: string[]): Promise<EmbeddingResult> {
    const host = this.host.replace(/\/$/, "");
    const payload = await postJson(`${host}/api/embed`, {
      model: this.model,
      input: texts,
    });
    const embeddings = (payload as { embeddings?: unknown }).embeddings;
    if (!Array.isArray(embeddings) || embeddings.length === 0) {
      throw new EngineError("EMBEDDING_UNREACHABLE", "Ollama returned no embeddings.");
    }
    const vectors = (embeddings as unknown[]).map((row) => (row as number[]).map(Number));
    const first = vectors[0];
    if (!first) throw new EngineError("EMBEDDING_UNREACHABLE", "Ollama returned no embeddings.");
    this.dimension ??= first.length;
    return { vectors, provider: this.name, model: this.model, dimension: first.length };
  }

  version(): string {
    return `ollama:${this.model}`;
  }
}

/** Deterministic test double. NOT a retrieval baseline. */
export class HashingEmbedder implements EmbeddingProvider {
  readonly name = "hashing";
  constructor(private readonly dimension = 64) {}

  async embed(texts: string[]): Promise<EmbeddingResult> {
    const vectors = texts.map((text) => {
      const vec = new Array<number>(this.dimension).fill(0);
      for (const token of text.toLowerCase().split(/\s+/)) {
        if (!token) continue;
        const digest = createHash("sha256").update(token, "utf8").digest();
        const index = digest.readUInt32BE(0) % this.dimension;
        vec[index] = (vec[index] ?? 0) + 1;
      }
      const norm = Math.sqrt(vec.reduce((acc, v) => acc + v * v, 0)) || 1;
      return vec.map((v) => v / norm);
    });
    return {
      vectors,
      provider: this.name,
      model: `hashing-${this.dimension}`,
      dimension: this.dimension,
    };
  }

  version(): string {
    return `hashing:${this.dimension}`;
  }
}

export type EmbeddingSpec = {
  provider: "ollama" | "sentence-transformers" | "hashing";
  model: string;
  host: string;
  dimension?: number;
};

export function buildEmbedder(spec: EmbeddingSpec): EmbeddingProvider {
  if (spec.provider === "ollama") return new OllamaEmbeddingProvider(spec.model, spec.host);
  if (spec.provider === "hashing") {
    if (process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS !== "1") {
      throw new EngineError(
        "CONFIG_INVALID",
        "the hashing embedder is a test double and is refused without REMEMBERING_ALLOW_TEST_EMBEDDINGS=1.",
      );
    }
    return new HashingEmbedder(spec.dimension ?? 64);
  }
  throw new EngineError(
    "CONFIG_INVALID",
    `embedding provider ${JSON.stringify(spec.provider)} is not supported by the TypeScript engine; use "ollama" (or the hashing test double under REMEMBERING_ALLOW_TEST_EMBEDDINGS=1).`,
  );
}

export async function checkOllamaModel(host: string, model: string): Promise<{ reachable: boolean; available: boolean; detail: string }> {
  const base = host.replace(/\/$/, "");
  let payload: { models?: Array<{ name?: string }> };
  try {
    const res = await fetch(`${base}/api/tags`, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = (await res.json()) as typeof payload;
  } catch (error) {
    throw new EngineError(
      "EMBEDDING_UNREACHABLE",
      `embedding provider Ollama is not reachable at ${host}: ${error instanceof Error ? `${error.name}: ${String(error.message).slice(0, 160)}` : String(error).slice(0, 160)}`,
    );
  }
  const names = (payload.models ?? []).map((m) => m.name ?? "");
  const found = names.some((n) => n === model || n === `${model}:latest` || n.startsWith(`${model}:`));
  if (!found) {
    throw new EngineError(
      "EMBEDDING_MODEL_MISSING",
      `embedding model ${JSON.stringify(model)} is not available in Ollama at ${host}. Run \`ollama pull ${model}\` with the exact configured model name; the adapter will not download or substitute another model.`,
    );
  }
  return { reachable: true, available: true, detail: `ollama model ${JSON.stringify(model)} present at ${host}` };
}

export function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    dot += x * y;
    na += x * x;
    nb += y * y;
  }
  return dot / ((Math.sqrt(na) || 1) * (Math.sqrt(nb) || 1));
}
