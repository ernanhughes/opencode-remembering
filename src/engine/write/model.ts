import { createHash, randomUUID } from "node:crypto";

import { EngineError } from "../errors";

export const WRITE_ENGINE_VERSION = "write-engine-v0.1";
export const WRITE_STORE_VERSION = "write-store-v0.1";
export const RECORD_SCHEMA_VERSION = "explicit-memory-record-v0.1";
export const ACTION_SCHEMA_VERSION = "memory-action-v0.1";
export const RELATION_VERSION = "memory-relation-v0.1";

export type RememberAction = "remember" | "correct" | "supersede" | "retract";
export type RememberRole =
  | "ordinary" | "evidence" | "proposal" | "preference" | "decision" | "production_state";
export const REMEMBER_ROLES: RememberRole[] = [
  "ordinary", "evidence", "proposal", "preference", "decision", "production_state",
];

export type RecordState = "active" | "corrected" | "superseded" | "retracted";

export type MemoryRecord = {
  recordId: string;
  content: string;
  role: RememberRole;
  sourceId: string;
  lineageRoot: string;
  standingCeiling: string;
  state: RecordState;
  callerScope: string;
  origin: string;
  evidenceRefs: string[];
  eventTime: string;
  effectiveFrom: string;
  createdAt: string;
};

export type MemoryActionRecord = {
  actionId: string;
  action: RememberAction;
  recordId: string | null;
  targetRecordId: string | null;
  reason: string;
  callerScope: string;
  origin: string;
  idempotencyKey: string | null;
  relation: { type: string; targetRecordId: string } | null;
  createdAt: string;
};

export type WriteAuthorization = {
  verdict: "allow" | "deny";
  reason: string;
  policyVersion: string;
  policyDigest: string;
  matchedGrantId: string | null;
};

export function newRecordId(): string {
  return `rec-${randomUUID().slice(0, 8)}`;
}

export function newActionId(): string {
  return `act-${randomUUID().slice(0, 8)}`;
}

export function memorySourceId(recordId: string): string {
  return `memory://${recordId}`;
}

export function lineageDigest(parts: string[]): string {
  return createHash("sha256").update(JSON.stringify(parts), "utf8").digest("hex").slice(0, 16);
}

export function validateWriteInput(input: {
  action?: string;
  content?: string;
  target_record_id?: string;
  role?: string;
  reason?: string;
  evidence_refs?: unknown;
  caller_scope?: string;
}): void {
  const action = input.action ?? "remember";
  if (action !== "remember" && action !== "correct" && action !== "supersede" && action !== "retract") {
    throw new EngineError("MEMORY_WRITE_DENIED", `unknown write action ${JSON.stringify(input.action)}.`);
  }
  if (input.role !== undefined && !(REMEMBER_ROLES as string[]).includes(input.role)) {
    throw new EngineError("MEMORY_WRITE_DENIED", `unknown memory role ${JSON.stringify(input.role)}.`);
  }
  if (action === "remember" && (!input.content || !input.content.trim())) {
    throw new EngineError("MEMORY_WRITE_DENIED", "remember requires non-empty content.");
  }
  if ((action === "correct" || action === "supersede" || action === "retract") && !input.target_record_id?.trim()) {
    throw new EngineError("MEMORY_TARGET_NOT_FOUND", `action '${action}' requires target_record_id.`);
  }
  if (input.caller_scope !== undefined && !input.caller_scope.trim()) {
    throw new EngineError("MEMORY_WRITE_DENIED", "caller_scope must be non-empty.");
  }
  if (input.evidence_refs !== undefined && !Array.isArray(input.evidence_refs)) {
    throw new EngineError("MEMORY_WRITE_DENIED", "evidence_refs must be a list.");
  }
}
