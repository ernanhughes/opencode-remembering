import { createHash } from "node:crypto";

import { EngineError } from "../errors";

export const TRUST_ENGINE_VERSION = "trust-engine-v0.1";
export const INSTRUCTION_SCREEN_VERSION = "instruction-screen-v0.1";

export type TrustLevel = "T0" | "S1" | "S2" | "S3" | "FULL";
export const TRUST_LEVELS: TrustLevel[] = ["T0", "S1", "S2", "S3", "FULL"];
export const TRUST_RANK: Record<TrustLevel, number> = { T0: 0, S1: 1, S2: 2, S3: 3, FULL: 4 };

export function parseTrustLevel(value: unknown): TrustLevel {
  if (typeof value === "string" && (TRUST_LEVELS as string[]).includes(value)) return value as TrustLevel;
  throw new EngineError("TRUST_POLICY_INVALID", `unknown trust level ${JSON.stringify(value)}.`);
}

export type TrustGrant = {
  id: string;
  sourcePattern: string;
  level: TrustLevel;
  role: string;
};

export type TrustPolicy = {
  version: string;
  digest: string;
  source: string;
  configured: boolean;
  defaultLevel: TrustLevel;
  defaultSourceClass: string;
  grants: TrustGrant[];
};

export type StandingTransition = "restrict" | "revoke" | "restore";

export type StandingEvent = {
  eventId: string;
  sourceId: string;
  transition: StandingTransition;
  level?: TrustLevel;
  reason: string;
  at: string;
};

export type StandingState = {
  revoked: Set<string>;
  restricted: Map<string, TrustLevel>;
};

export type TrustVerdict = "allow" | "deny" | "quarantine";

export type TrustDecision = {
  sourceId: string;
  verdict: TrustVerdict;
  reason: string;
  matchedGrantId: string | null;
  effectiveLevel: TrustLevel;
};

export function digestTrustPolicy(canonical: { version: string; defaultLevel: string; grants: Array<{ id: string; sourcePattern: string; level: string }> }): string {
  return createHash("sha256").update(JSON.stringify(canonical), "utf8").digest("hex").slice(0, 16);
}

export function matchSourcePattern(pattern: string, sourceId: string): boolean {
  if (pattern === "*") return true;
  if (pattern.endsWith("*")) return sourceId.startsWith(pattern.slice(0, -1));
  return pattern === sourceId;
}
