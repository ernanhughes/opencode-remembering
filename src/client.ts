import { execFile } from "node:child_process";
import { promisify } from "node:util";

import type { RememberingConfig } from "./config";

const execFileAsync = promisify(execFile);

export type DoctorResult = {
  ok: boolean;
  project_memory_root: string;
  dsn_redacted: string;
  schema: string;
  pgvector_version: string;
  indexed: boolean;
  chunks: number | null;
};

export type SearchItem = {
  chunk_id: string;
  source_id: string;
  section: string | null;
  rank: number;
  score: number;
  text: string;
};

export type SearchResult = {
  ok: boolean;
  indexed: boolean;
  schema: string;
  items: SearchItem[];
  message?: string;
};

export type ContextResult = SearchResult & {
  trace_id: string;
  content: string;
  chars: number;
};

export class ProjectMemoryClient {
  constructor(
    private readonly config: RememberingConfig,
    private readonly projectDirectory: string,
  ) {}

  private async call<T>(action: string, payload: Record<string, unknown> = {}): Promise<T> {
    const input = JSON.stringify({
      ...payload,
      schema: this.config.schema,
      project_directory: this.projectDirectory,
    });

    try {
      const { stdout, stderr } = await execFileAsync(
        this.config.python,
        [this.config.bridgePath, action],
        {
          input,
          timeout: 30_000,
          maxBuffer: 4 * 1024 * 1024,
          env: {
            ...process.env,
            PROJECT_MEMORY_ROOT: this.config.projectMemoryRoot,
            MEMORY_BASELINE_DSN: this.config.dsn,
          },
        },
      );
      if (stderr.trim()) {
        console.warn(`[opencode-remembering] bridge stderr: ${stderr.trim()}`);
      }
      return JSON.parse(stdout) as T;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`Project Memory bridge failed (${action}): ${detail}`, {
        cause: error,
      });
    }
  }

  doctor(): Promise<DoctorResult> {
    return this.call<DoctorResult>("doctor");
  }

  search(query: string, limit = 8): Promise<SearchResult> {
    return this.call<SearchResult>("search", { query, limit });
  }

  context(query: string, maxChars: number, maxResults: number): Promise<ContextResult> {
    return this.call<ContextResult>("context", {
      query,
      max_chars: maxChars,
      max_results: maxResults,
    });
  }
}
