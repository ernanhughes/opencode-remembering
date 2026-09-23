/** Coded failure with an actionable message (never contains secrets). */
export class EngineError extends Error {
  readonly code: string;
  readonly detail: string;
  constructor(code: string, message: string) {
    super(`${code}: ${message}`);
    this.name = "EngineError";
    this.code = code;
    this.detail = message;
  }
}

export const SCHEMA_PATTERN = /^[A-Za-z_][A-Za-z0-9_$]*$/;

export function assertSchemaName(schema: string): string {
  const name = schema.trim();
  if (!name || !SCHEMA_PATTERN.test(name) || name.length > 63) {
    throw new EngineError(
      "CONFIG_INVALID",
      `refusing unsafe schema name ${JSON.stringify(schema)}; refusing to fall back to a shared schema.`,
    );
  }
  if (name.toLowerCase().startsWith("pg_")) {
    throw new EngineError(
      "CONFIG_INVALID",
      `refusing reserved schema name ${JSON.stringify(schema)}.`,
    );
  }
  return name;
}

export function classifyConnectionError(error: unknown): EngineError {
  const message = error instanceof Error ? error.message : String(error);
  const code = (error as { code?: string })?.code;
  // node-postgres uses code 3D000 for missing database.
  if (code === "3D000" || (/does not exist/i.test(message) && /database/i.test(message))) {
    return new EngineError(
      "DB_MISSING",
      "PostgreSQL server is reachable, but the configured database does not exist. " +
        "Create the database or change the configured DSN. Tables and schemas are created by memory_setup; the database itself is not.",
    );
  }
  return new EngineError(
    "DB_UNREACHABLE",
    `cannot reach PostgreSQL: ${error instanceof Error ? error.name : "Error"}: ${message.slice(0, 200)}`,
  );
}

export function redactDsn(value: string): string {
  if (!value.includes("@")) return value;
  const at = value.lastIndexOf("@");
  const prefix = value.slice(0, at);
  const suffix = value.slice(at + 1);
  const scheme = prefix.includes("://") ? prefix.split("://", 1)[0] : "postgresql";
  return `${scheme}://***:***@${suffix}`;
}
