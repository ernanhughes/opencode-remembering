import { createHash, randomUUID } from "node:crypto";

export const TRACE_ENGINE_VERSION = "trace-engine-v0.1";
export const TRACE_SCHEMA_VERSION = "context-trace-v0.1";
export const TRACE_STORE_VERSION = "trace-store-v0.1";
export const REPLAY_PROTOCOL_VERSION = "trace-replay-v0.1";

export type TraceCandidate = {
  candidateId: string;
  sourceId: string;
  text: string;
  score: number;
  /** Stage-by-stage admission record. */
  stages: Record<string, { admitted: boolean; reason: string }>;
  terminalStage: string;
};

export type TracePolicyRefs = {
  retrieval: string;
  temporal: string;
  frame: string;
  trust: string;
  trustDigest: string | null;
  selection: string;
};

export type ContextTraceRecord = {
  traceId: string;
  query: string;
  route: string;
  workType: string | null;
  createdAt: string;
  candidates: TraceCandidate[];
  policies: TracePolicyRefs;
  budget: { maxChars: number; maxResults: number };
  digest: string;
  replayOf: string | null;
};

export function traceDigest(trace: Omit<ContextTraceRecord, "digest" | "traceId"> & { traceId?: string }): string {
  const canonical = JSON.stringify([
    trace.query, trace.route, trace.workType,
    trace.candidates.map((c) => [c.candidateId, c.sourceId, c.terminalStage, c.score]),
    trace.policies, trace.budget, trace.replayOf ?? null,
  ]);
  return createHash("sha256").update(canonical, "utf8").digest("hex").slice(0, 16);
}

export function createTrace(input: {
  query: string;
  route: string;
  workType?: string | null;
  candidates: TraceCandidate[];
  policies: TracePolicyRefs;
  budget: { maxChars: number; maxResults: number };
  createdAt?: string;
  replayOf?: string | null;
  traceId?: string;
}): ContextTraceRecord {
  const base = {
    query: input.query,
    route: input.route,
    workType: input.workType ?? null,
    createdAt: input.createdAt ?? new Date().toISOString(),
    candidates: input.candidates,
    policies: input.policies,
    budget: input.budget,
    replayOf: input.replayOf ?? null,
  };
  const traceId = input.traceId ?? `trace-${randomUUID().slice(0, 8)}`;
  return { ...base, traceId, digest: traceDigest({ ...base, traceId }) };
}

export function verifyTrace(trace: ContextTraceRecord): { valid: boolean; reason: string } {
  const recomputed = traceDigest(trace);
  if (recomputed !== trace.digest) {
    return { valid: false, reason: `digest mismatch: stored ${trace.digest}, recomputed ${recomputed}.` };
  }
  if (!trace.traceId.trim() || !trace.query.trim()) {
    return { valid: false, reason: "trace requires trace_id and query." };
  }
  return { valid: true, reason: "digest matches; required fields present." };
}
