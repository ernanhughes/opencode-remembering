import { spawn } from "node:child_process";

import type { RememberingConfig } from "./config";

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

  private call<T>(action: string, payload: Record<string, unknown> = {}): Promise<T> {
    const input = JSON.stringify({
      ...payload,
      schema: this.config.schema,
      project_directory: this.projectDirectory,
    });

    return new Promise<T>((resolve, reject) => {
      const child = spawn(
        this.config.python,
        [this.config.bridgePath, action],
        {
          windowsHide: true,
          env: {
            ...process.env,
            PROJECT_MEMORY_ROOT: this.config.projectMemoryRoot,
            MEMORY_BASELINE_DSN: this.config.dsn,
          },
          stdio: ["pipe", "pipe", "pipe"],
        },
      );

      let stdout = "";
      let stderr = "";
      const timeout = setTimeout(() => {
        child.kill();
        reject(new Error(`Project Memory bridge timed out (${action})`));
      }, 30_000);

      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk: string) => {
        stdout += chunk;
      });
      child.stderr.on("data", (chunk: string) => {
        stderr += chunk;
      });

      child.on("error", (error) => {
        clearTimeout(timeout);
        reject(
          new Error(`Project Memory bridge failed to start (${action}): ${error.message}`, {
            cause: error,
          }),
        );
      });

      child.on("close", (code) => {
        clearTimeout(timeout);
        if (code !== 0) {
          reject(
            new Error(
              `Project Memory bridge failed (${action}, exit=${code}): ${stderr.trim() || stdout.trim()}`,
            ),
          );
          return;
        }

        if (stderr.trim()) {
          console.warn(`[opencode-remembering] bridge stderr: ${stderr.trim()}`);
        }

        try {
          resolve(JSON.parse(stdout) as T);
        } catch (error) {
          reject(
            new Error(
              `Project Memory bridge returned invalid JSON (${action}): ${stdout.slice(0, 500)}`,
              { cause: error },
            ),
          );
        }
      });

      child.stdin.end(input);
    });
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
