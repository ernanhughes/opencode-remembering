import * as crypto from "node:crypto";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

export type RememberingConfig = {
  projectMemoryRoot: string;
  dsn: string;
  python: string;
  schema: string;
  bridgePath: string;
  context: {
    autoInject: boolean;
    maxChars: number;
    maxResults: number;
  };
};

type RawConfig = {
  project_memory_root?: unknown;
  dsn?: unknown;
  python?: unknown;
  schema?: unknown;
  context?: {
    auto_inject?: unknown;
    max_chars?: unknown;
    max_results?: unknown;
  };
};

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function positiveInteger(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : fallback;
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
  const normalized = process.platform === "win32" ? canonical.toLowerCase() : canonical;
  const digest = crypto.createHash("sha256").update(normalized).digest("hex").slice(0, 12);
  return `remembering_${digest}`;
}

async function readRawConfig(): Promise<RawConfig> {
  const configPath = path.join(os.homedir(), ".config", "opencode", "remembering.json");
  try {
    const parsed = JSON.parse(await fs.readFile(configPath, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("configuration root must be an object");
    }
    return parsed as RawConfig;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw new Error(`Invalid OpenCode Remembering config at ${configPath}: ${String(error)}`);
  }
}

export async function loadConfig(directory: string): Promise<RememberingConfig> {
  const raw = await readRawConfig();
  const configuredRoot =
    nonEmptyString(process.env.PROJECT_MEMORY_ROOT) ??
    nonEmptyString(raw.project_memory_root);
  const projectMemoryRoot = path.resolve(
    configuredRoot ?? path.join(directory, "..", "project-memory"),
  );

  const dsn =
    nonEmptyString(process.env.MEMORY_BASELINE_DSN) ??
    nonEmptyString(raw.dsn) ??
    "postgresql://postgres:postgres@localhost:5434/memory_baseline";

  const python = nonEmptyString(raw.python) ?? process.env.PYTHON ?? "python";
  const schema = nonEmptyString(raw.schema) ?? await schemaForProject(directory);
  const autoInject =
    typeof raw.context?.auto_inject === "boolean"
      ? raw.context.auto_inject
      : true;

  return {
    projectMemoryRoot,
    dsn,
    python,
    schema,
    bridgePath: fileURLToPath(new URL("../bridge/remembering_bridge.py", import.meta.url)),
    context: {
      autoInject,
      maxChars: positiveInteger(raw.context?.max_chars, 4000),
      maxResults: positiveInteger(raw.context?.max_results, 6),
    },
  };
}
