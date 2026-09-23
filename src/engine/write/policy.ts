import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import * as path from "node:path";

import { EngineError } from "../errors";
import type { RememberRole, WriteAuthorization } from "./model";

export type WritePolicy = {
  version: string;
  digest: string;
  source: string;
  configured: boolean;
  /** Roles that require evidence references. */
  requireEvidenceFor: RememberRole[];
  /** Roles a given scope may not write; empty means no restriction. */
  deniedRoles: RememberRole[];
};

export const BUILTIN_WRITE_VERSION = "builtin-write-default-v0.1";

export function builtinWritePolicy(): WritePolicy {
  const requireEvidenceFor: RememberRole[] = ["production_state"];
  const policy = {
    version: BUILTIN_WRITE_VERSION,
    source: "builtin_default",
    configured: false,
    requireEvidenceFor,
    deniedRoles: [] as RememberRole[],
  };
  return { ...policy, digest: createHash("sha256").update(JSON.stringify([policy.version, requireEvidenceFor]), "utf8").digest("hex").slice(0, 16) };
}

export function authorizeWrite(
  policy: WritePolicy,
  input: { role: RememberRole; evidenceRefs: string[]; callerScope: string; origin: string },
): WriteAuthorization {
  if (input.origin !== "opencode" && input.origin !== "cli") {
    return { verdict: "deny", reason: `unknown origin ${JSON.stringify(input.origin)}.`, policyVersion: policy.version, policyDigest: policy.digest, matchedGrantId: null };
  }
  if (!input.callerScope.trim()) {
    return { verdict: "deny", reason: "caller_scope is required.", policyVersion: policy.version, policyDigest: policy.digest, matchedGrantId: null };
  }
  if (policy.deniedRoles.includes(input.role)) {
    return { verdict: "deny", reason: `role ${input.role} is denied by write policy.`, policyVersion: policy.version, policyDigest: policy.digest, matchedGrantId: null };
  }
  if (policy.requireEvidenceFor.includes(input.role) && input.evidenceRefs.length === 0) {
    return { verdict: "deny", reason: `role ${input.role} requires evidence_refs.`, policyVersion: policy.version, policyDigest: policy.digest, matchedGrantId: null };
  }
  return { verdict: "allow", reason: "authorized by write policy.", policyVersion: policy.version, policyDigest: policy.digest, matchedGrantId: "default" };
}

export async function loadWritePolicy(projectDirectory: string): Promise<{ policy: WritePolicy; valid: boolean; error: string | null }> {
  const file = path.join(projectDirectory, ".remembering", "write-policy.json");
  let content: string;
  try {
    content = await readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { policy: builtinWritePolicy(), valid: true, error: null };
    throw error;
  }
  let raw: unknown;
  try {
    raw = JSON.parse(content);
  } catch {
    return { policy: builtinWritePolicy(), valid: false, error: `write policy at ${file} is not valid JSON.` };
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { policy: builtinWritePolicy(), valid: false, error: `write policy at ${file} must be an object.` };
  }
  const r = raw as Record<string, unknown>;
  const version = typeof r["version"] === "string" ? (r["version"] as string) : "explicit-v0.1";
  const requireEvidenceFor: RememberRole[] = Array.isArray(r["require_evidence_for"])
    ? (r["require_evidence_for"] as unknown[]).map((v) => {
      if (typeof v !== "string") throw new EngineError("WRITE_POLICY_INVALID", "require_evidence_for must be role names.");
      return v as RememberRole;
    })
    : ["production_state"];
  const policy: WritePolicy = {
    version, source: file, configured: true, requireEvidenceFor,
    deniedRoles: Array.isArray(r["denied_roles"]) ? (r["denied_roles"] as RememberRole[]) : [],
    digest: "",
  };
  policy.digest = createHash("sha256").update(JSON.stringify([version, requireEvidenceFor]), "utf8").digest("hex").slice(0, 16);
  return { policy, valid: true, error: null };
}
