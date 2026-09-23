import type { Info as ToolInfo } from "@opencode/plugin/promise/tool";

import type { ProjectMemoryClient } from "./client";

export function MemoryHealth(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_health",
    description:
      "Check Project Memory, PostgreSQL, pgvector, and this project's isolated retrieval schema.",
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

export function MemorySearch(client: ProjectMemoryClient): ToolInfo {
  return {
    name: "memory_search",
    description:
      "Search this project's Project Memory index. Bootstrap implementation uses PostgreSQL full-text retrieval; hybrid pgvector retrieval is the next milestone.",
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
      "Build a bounded project-memory bundle for the current task. This is the bootstrap seam before full ProjectFrame/WorkFrame/trust/ContextTrace integration.",
    input: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1 },
        max_chars: { type: "integer", minimum: 256, maximum: 20000 },
        max_results: { type: "integer", minimum: 1, maximum: 20 },
      },
      required: ["query"],
      additionalProperties: false,
    },
    async execute(input) {
      const args = input as {
        query: string;
        max_chars?: number;
        max_results?: number;
      };
      const result = await client.context(
        args.query,
        args.max_chars ?? 4000,
        args.max_results ?? 6,
      );
      return { content: JSON.stringify(result, null, 2) };
    },
  };
}
