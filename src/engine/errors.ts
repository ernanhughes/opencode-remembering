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

/** Redact credentials from a DSN for display. The user and host stay visible. */
export function redactDsn(value: string): string {
  const schemeSplit = value.split("://");
  if (schemeSplit.length < 2) return value;
  const scheme = schemeSplit[0];
  const rest = schemeSplit.slice(1).join("://");
  const at = rest.lastIndexOf("@");
  if (at === -1) return value;
  const authority = rest.slice(0, at);
  const suffix = rest.slice(at + 1);
  const colon = authority.indexOf(":");
  const user = colon === -1 ? authority : authority.slice(0, colon);
  if (!user) return `${scheme}://***@${suffix}`;
  return colon === -1 ? `${scheme}://${user}@${suffix}` : `${scheme}://${user}:***@${suffix}`;
}

/** Security/integrity failures must fail closed and never trigger JSON fallback. */
export const SECURITY_FAILURE_CODES = new Set([
  "SCHEMA_MISMATCH",
  "STORE_PROJECT_MISMATCH",
  "CONFIG_INVALID",
  "FRAME_PROJECT_MISMATCH",
  "HTTP_BACKEND_AUTH",
  "STORE_VERSION_MISMATCH",
]);

export function isSecurityFailureCode(code: string | undefined): boolean {
  return !!code && SECURITY_FAILURE_CODES.has(code);
}

export function isSecurityFailure(error: unknown): boolean {
  const code = (error as { code?: string })?.code;
  if (isSecurityFailureCode(code)) return true;
  const message = String((error as Error)?.message ?? error);
  return /SCHEMA_MISMATCH|STORE_PROJECT_MISMATCH|CONFIG_INVALID|FRAME_PROJECT_MISMATCH|HTTP_BACKEND_AUTH|STORE_VERSION_MISMATCH/.test(message);
}

/** Availability failures may trigger fallback to the JSON backend. */
export function isAvailabilityFailure(error: unknown): boolean {
  const code = (error as { code?: string })?.code;
  return code === "DB_UNREACHABLE" || code === "DB_MISSING" || code === "HTTP_BACKEND_UNREACHABLE" || code === "HTTP_BACKEND_PROTOCOL" || code === "JSON_STORE_UNAVAILABLE" || code === "EMBEDDING_UNREACHABLE";
}

/** Strip bearer tokens / secrets from any string destined for logs or traces. */
export function redactSecrets(value: string): string {
  return value
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/-]+=*(\s|$)/gi, "$1***$2")
    .replace(/("?(authorization|api[_-]?key|token|secret)"?\s*[:=]\s*"?)[^",\s}]+/gi, "$1***");
}
