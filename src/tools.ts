import type { Info as ToolInfo } from "@opencode/plugin/promise/tool";

import type {
  ProjectMemoryClient,
  RouteRequest,
  TemporalStandpointRequest,
  TrustRequest,
  WorkRequest,
} from "./client";

export function MemoryHealth(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_health",
    description:
      "Check remembering engine readiness: PostgreSQL reachability, pgvector/pg_trgm, " +
      "project schema initialization, embedding provider and model, stored vs " +
      "configured embedding compatibility, index presence, and source/chunk counts. " +
      "Explains exactly what to do next (usually memory_setup).",
    input: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    async execute() {
      const result = await client.doctor();
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemorySetup(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_setup",
    description:
      "Initialize this project's isolated memory schema without any manual SQL: " +
      "validates the checkout, connects to PostgreSQL, enables pgvector/pg_trgm, " +
      "probes the configured embedding model, creates tables and indexes, ingests " +
      "the repository, and verifies the store. Idempotent; safe to re-run.",
    input: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    async execute() {
      const result = await client.setup();
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemoryRefresh(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_refresh",
    description:
      "Re-ingest this project's repository through the bundled ingestion " +
      "(discover, parse, chunk, embed, upsert). Reports discovered/indexed/unchanged/ " +
      "added/changed/removed/chunks/embedded/failed. Idempotent: makes it obvious " +
      "when nothing changed.",
    input: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    async execute() {
      const result = await client.refresh();
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemorySearch(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_search",
    description:
      "Search this project's memory index with hybrid retrieval " +
      "(PostgreSQL full-text + pgvector dense + reciprocal-rank fusion). Returns " +
      "provenance-bearing chunks plus a retrieval trace proving the dense stage ran.",
    input: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1 },
        limit: { type: "integer", minimum: 1, maximum: 20 },
      },
      required: ["query"],
      additionalProperties: false,
    },
    async execute(input) {
      const args = input as { query: string; limit?: number };
      const result = await client.search(args.query, args.limit ?? 8);
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemoryContext(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_context",
    description:
      "Build a bounded memory bundle from hybrid retrieval for the current " +
      "task. Accepts route auto (default), recall, or influence; optional " +
      "temporal standpoint, work/frame declaration, and trust options " +
      "(caller_scope, evaluation level). Influence bundles contain " +
      "admitted candidates only; denied/quarantined evidence stays in " +
      "the trace.",
    input: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1 },
        route: { type: "string", enum: ["auto", "recall", "influence"] },
        temporal: {
          type: "object",
          properties: {
            mode: {
              type: "string",
              enum: ["current", "valid_at", "as_known", "bitemporal"],
            },
            valid_at: { type: "string" },
            known_at: { type: "string" },
          },
          additionalProperties: false,
        },
        work: {
          type: "object",
          properties: {
            mode: { type: "string", enum: ["auto", "explicit", "none"] },
            work_type: { type: "string" },
            objective: { type: "string" },
            prior_work_type: { type: "string" },
          },
          additionalProperties: false,
        },
        trust: {
          type: "object",
          properties: {
            caller_scope: { type: "string" },
            level: { type: "string", enum: ["T0", "S1", "S2", "S3", "FULL"] },
          },
          additionalProperties: false,
        },
        max_chars: { type: "integer", minimum: 256, maximum: 20000 },
        max_results: { type: "integer", minimum: 1, maximum: 20 },
      },
      required: ["query"],
      additionalProperties: false,
    },
    async execute(input) {
      const args = input as {
        query: string;
        route?: RouteRequest;
        temporal?: TemporalStandpointRequest;
        work?: WorkRequest;
        trust?: TrustRequest;
        max_chars?: number;
        max_results?: number;
      };
      const result = await client.context(
        args.query,
        args.max_chars ?? 4000,
        args.max_results ?? 6,
        args.route ?? "auto",
        args.temporal,
        args.work,
        args.trust,
      );
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemoryState(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_state",
    description:
      "Inspect resolved temporal state for one subject (current, valid_at, " +
      "as_known, or bitemporal standpoint) with trajectory and provenance. " +
      "Temporal validity is not trustworthiness.",
    input: {
      type: "object",
      properties: {
        subject: { type: "string", minLength: 1 },
        temporal: {
          type: "object",
          properties: {
            mode: {
              type: "string",
              enum: ["current", "valid_at", "as_known", "bitemporal"],
            },
            valid_at: { type: "string" },
            known_at: { type: "string" },
          },
          additionalProperties: false,
        },
      },
      required: ["subject"],
      additionalProperties: false,
    },
    async execute(input) {
      const args = input as {
        subject: string;
        temporal?: TemporalStandpointRequest;
      };
      const result = await client.state(args.subject, {
        temporal: args.temporal,
      });
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}

export function MemoryTemporalImport(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_temporal_import",
    description:
      "Import explicit structured temporal events from " +
      ".remembering/temporal/events.jsonl into this project's temporal store. " +
      "Idempotent; malformed lines fail visibly.",
    input: {
      type: "object",
      properties: {},
      additionalProperties: false,
    },
    async execute() {
      const result = await client.temporalImport();
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}
