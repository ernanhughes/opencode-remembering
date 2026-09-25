import type { Pool, PoolClient } from "pg";

import { assertSchemaName, classifyConnectionError, EngineError } from "./errors";

/** Quote a PostgreSQL identifier (schema/table). Validated upstream. */
export function ident(schema: string): string {
  assertSchemaName(schema);
  return `"${schema.replace(/"/g, '""')}"`;
}

export function toRegclassParam(schema: string, relation: string): string {
  assertSchemaName(schema);
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(relation)) {
    throw new EngineError("CONFIG_INVALID", `refusing unsafe relation name ${JSON.stringify(relation)}.`);
  }
  return `${schema}.${relation}`;
}

export async function connectPool(dsn: string): Promise<Pool> {
  // Lazy pg import: HTTP/JSON-only installs never load the driver.
  let PgPool: new (config: { connectionString: string; connectionTimeoutMillis: number }) => Pool;
  try {
    ({ Pool: PgPool } = await import("pg"));
  } catch {
    throw new EngineError(
      "DB_UNREACHABLE",
      "direct PostgreSQL was requested but the optional 'pg' driver is not installed. Install it (bun add pg) or switch storage.mode to \"http\" or \"json\".",
    );
  }
  const pool: Pool = new PgPool({ connectionString: dsn, connectionTimeoutMillis: 10_000 });
  try {
    const client = await pool.connect();
    client.release();
    return pool;
  } catch (error) {
    await pool.end().catch(() => {});
    throw classifyConnectionError(error);
  }
}

export async function withClient<T>(pool: Pool, fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect().catch((error: unknown) => {
    throw classifyConnectionError(error);
  });
  try {
    return await fn(client);
  } finally {
    client.release();
  }
}

export type ExtensionStatus = {
  installed: boolean;
  version: string | null;
  software: boolean;
};

export async function extensionStatus(client: PoolClient, name: string): Promise<ExtensionStatus> {
  const installed = await client.query("SELECT extversion FROM pg_extension WHERE extname = $1", [name]);
  if (installed.rowCount && installed.rowCount > 0) {
    return { installed: true, version: String(installed.rows[0].extversion), software: true };
  }
  const available = await client.query(
    "SELECT default_version FROM pg_available_extensions WHERE name = $1",
    [name],
  );
  if (!available.rowCount) return { installed: false, version: null, software: false };
  return { installed: false, version: null, software: true };
}

export async function ensureExtension(client: PoolClient, name: string): Promise<ExtensionStatus> {
  const status = await extensionStatus(client, name);
  if (status.installed) return status;
  if (!status.software) {
    throw new EngineError(
      "EXTENSION_UNAVAILABLE",
      `extension ${JSON.stringify(name)} is not installed in this PostgreSQL installation (pg_available_extensions has no such entry). Install the extension software first (e.g. the pgvector package for your server), then retry.`,
    );
  }
  if (name !== "vector" && name !== "pg_trgm") {
    throw new EngineError("CONFIG_INVALID", `refusing to enable unexpected extension ${JSON.stringify(name)}.`);
  }
  try {
    await client.query(`CREATE EXTENSION IF NOT EXISTS "${name.replace(/"/g, "")}"`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const code = (error as { code?: string })?.code;
    if (code === "42501" || /permission denied/i.test(message)) {
      throw new EngineError(
        "EXTENSION_PERMISSION",
        `permission denied enabling extension ${JSON.stringify(name)}. Ask a database superuser to run CREATE EXTENSION ${name}; the configured role cannot enable it.`,
      );
    }
    throw new EngineError(
      "EXTENSION_UNAVAILABLE",
      `could not enable extension ${JSON.stringify(name)}: ${error instanceof Error ? error.name : "Error"}: ${message.slice(0, 200)}`,
    );
  }
  return extensionStatus(client, name);
}
