/**
 * Machine-readable readiness contract for the native doctor.
 * Pure logic: no database, no network, no wall clock.
 */

export type ReadinessCode =
  | "HEALTHY"
  | "NOT_INITIALIZED"
  | "BACKEND_UNREACHABLE"
  | "EXTENSION_MISSING"
  | "EMBEDDING_UNAVAILABLE"
  | "DIMENSION_MISMATCH"
  | "SCHEMA_MISMATCH"
  | "POLICY_INVALID"
  | "SUBSYSTEM_FAILURE";

export type Readiness = {
  ok: boolean;
  code: ReadinessCode;
  reasons: string[];
};

export type BaselineReadinessInput = {
  postgresReachable: boolean;
  databaseExists: boolean;
  pgvectorAvailable: boolean;
  pgTrgmAvailable: boolean;
  schemaInitialized: boolean;
  schemaIdentityOk: boolean;
  embeddingProviderReachable: boolean;
  embeddingModelAvailable: boolean;
  /** Null when there is nothing stored to compare against (fresh schema). */
  embeddingDimensionCompatible: boolean | null;
};

/**
 * Explicit readiness calculation. A null dimension verdict never fails:
 * NOT_INITIALIZED already covers the fresh-schema case, and an
 * initialized-but-empty store has nothing to conflict with.
 */
export function computeBaselineReadiness(input: BaselineReadinessInput): Readiness {
  const reasons: string[] = [];
  if (!input.postgresReachable) reasons.push("postgresql unreachable");
  if (!input.databaseExists) reasons.push("database does not exist");
  if (reasons.length > 0) {
    return { ok: false, code: "BACKEND_UNREACHABLE", reasons };
  }
  if (!input.schemaIdentityOk) {
    return {
      ok: false,
      code: "SCHEMA_MISMATCH",
      reasons: ["schema is claimed by a different project; refusing to mix projects"],
    };
  }
  if (!input.pgvectorAvailable) reasons.push("pgvector extension not enabled");
  if (!input.pgTrgmAvailable) reasons.push("pg_trgm extension not enabled");
  if (reasons.length > 0) {
    return { ok: false, code: "EXTENSION_MISSING", reasons };
  }
  if (!input.schemaInitialized) {
    return { ok: false, code: "NOT_INITIALIZED", reasons: ["schema not initialized; run setup"] };
  }
  if (!input.embeddingProviderReachable) reasons.push("embedding provider unreachable");
  if (!input.embeddingModelAvailable) reasons.push("embedding model unavailable");
  if (reasons.length > 0) {
    return { ok: false, code: "EMBEDDING_UNAVAILABLE", reasons };
  }
  if (input.embeddingDimensionCompatible === false) {
    return {
      ok: false,
      code: "DIMENSION_MISMATCH",
      reasons: ["stored vectors differ in dimension from the configured embedding model; re-embed with a fresh schema"],
    };
  }
  return { ok: true, code: "HEALTHY", reasons: [] };
}

export type SubsystemReadinessInput = {
  temporalReady: boolean;
  standingReady: boolean;
  trustPolicyValid: boolean;
  trustPolicyError?: string | null;
  traceReady: boolean;
  loopsReady: boolean;
  loopsError?: string;
  writesReady: boolean;
  writePolicyValid: boolean;
  writePolicyError?: string | null;
  frameError?: string | null;
};

/** Whole-product readiness: baseline gates everything, then policies, then stores. */
export function computeProductReadiness(
  baseline: Readiness,
  subsystems: SubsystemReadinessInput,
): Readiness {
  if (!baseline.ok) return baseline;
  const policyReasons: string[] = [];
  if (!subsystems.trustPolicyValid) {
    policyReasons.push(`trust policy invalid: ${(subsystems.trustPolicyError ?? "see trust section").slice(0, 160)}`);
  }
  if (!subsystems.writePolicyValid) {
    policyReasons.push(`write policy invalid: ${(subsystems.writePolicyError ?? "see writes section").slice(0, 160)}`);
  }
  if (subsystems.frameError) {
    policyReasons.push(`project frame invalid: ${subsystems.frameError.slice(0, 160)}`);
  }
  if (policyReasons.length > 0) {
    return { ok: false, code: "POLICY_INVALID", reasons: policyReasons };
  }
  const storeReasons: string[] = [];
  if (!subsystems.temporalReady) storeReasons.push("temporal store not ready");
  if (!subsystems.standingReady) storeReasons.push("standing store not ready");
  if (!subsystems.traceReady) storeReasons.push("trace store not ready");
  if (!subsystems.loopsReady) {
    storeReasons.push(
      subsystems.loopsError ? `open-loop projection failed: ${subsystems.loopsError.slice(0, 160)}` : "open-loop store not ready",
    );
  }
  if (!subsystems.writesReady) storeReasons.push("write store not ready");
  if (storeReasons.length > 0) {
    return { ok: false, code: "SUBSYSTEM_FAILURE", reasons: storeReasons };
  }
  return { ok: true, code: "HEALTHY", reasons: [] };
}
