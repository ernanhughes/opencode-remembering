import { readFile } from "node:fs/promises";
import * as path from "node:path";

import { EngineError } from "../errors";
import {
  digestTrustPolicy,
  parseTrustLevel,
  TRUST_ENGINE_VERSION,
  type TrustGrant,
  type TrustPolicy,
} from "./model";

export const BUILTIN_TRUST_VERSION = "builtin-default-v0.1";

export function builtinTrustPolicy(): TrustPolicy {
  const grants: TrustGrant[] = [];
  return {
    version: BUILTIN_TRUST_VERSION,
    digest: digestTrustPolicy({ version: BUILTIN_TRUST_VERSION, defaultLevel: "S1", grants: [] }),
    source: "builtin_default",
    configured: false,
    defaultLevel: "S1",
    defaultSourceClass: "ordinary",
    grants,
  };
}

export function validateTrustPolicy(raw: unknown, source: string): TrustPolicy {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new EngineError("TRUST_POLICY_INVALID", `trust policy at ${source} must be an object.`);
  }
  const r = raw as Record<string, unknown>;
  const version = typeof r["version"] === "string" && (r["version"] as string).trim()
    ? (r["version"] as string).trim()
    : "explicit-v0.1";
  const defaultLevel = parseTrustLevel(r["default_level"] ?? r["defaultLevel"] ?? "S1");
  const rawGrants = r["grants"] ?? [];
  if (!Array.isArray(rawGrants)) throw new EngineError("TRUST_POLICY_INVALID", "trust policy grants must be a list.");
  const grants: TrustGrant[] = rawGrants.map((g, i) => {
    if (!g || typeof g !== "object") throw new EngineError("TRUST_POLICY_INVALID", `grant ${i} must be an object.`);
    const grant = g as Record<string, unknown>;
    if (typeof grant["source_pattern"] !== "string" || !(grant["source_pattern"] as string).trim()) {
      if (typeof grant["sourcePattern"] !== "string" || !(grant["sourcePattern"] as string).trim()) {
        throw new EngineError("TRUST_POLICY_INVALID", `grant ${i} requires source_pattern.`);
      }
    }
    return {
      id: typeof grant["id"] === "string" && (grant["id"] as string).trim() ? (grant["id"] as string) : `grant-${i}`,
      sourcePattern: ((grant["source_pattern"] ?? grant["sourcePattern"]) as string).trim(),
      level: parseTrustLevel(grant["level"] ?? "S1"),
      role: typeof grant["role"] === "string" ? (grant["role"] as string) : "ordinary",
    };
  });
  const seen = new Set<string>();
  for (const g of grants) {
    if (seen.has(g.id)) throw new EngineError("TRUST_POLICY_INVALID", `duplicate grant id ${JSON.stringify(g.id)}.`);
    seen.add(g.id);
  }
  return {
    version,
    digest: digestTrustPolicy({ version, defaultLevel, grants: grants.map((g) => ({ id: g.id, sourcePattern: g.sourcePattern, level: g.level })) }),
    source,
    configured: true,
    defaultLevel,
    defaultSourceClass: typeof r["default_source_class"] === "string" ? (r["default_source_class"] as string) : "ordinary",
    grants,
  };
}

export async function loadTrustPolicy(projectDirectory: string): Promise<{ policy: TrustPolicy; valid: boolean; error: string | null }> {
  const file = path.join(projectDirectory, ".remembering", "trust-policy.json");
  let content: string;
  try {
    content = await readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { policy: builtinTrustPolicy(), valid: true, error: null };
    }
    throw error;
  }
  let raw: unknown;
  try {
    raw = JSON.parse(content);
  } catch {
    return { policy: builtinTrustPolicy(), valid: false, error: `trust policy at ${file} is not valid JSON.` };
  }
  try {
    return { policy: validateTrustPolicy(raw, file), valid: true, error: null };
  } catch (error) {
    return { policy: builtinTrustPolicy(), valid: false, error: error instanceof Error ? error.message.slice(0, 300) : String(error).slice(0, 300) };
  }
}

export { TRUST_ENGINE_VERSION };
