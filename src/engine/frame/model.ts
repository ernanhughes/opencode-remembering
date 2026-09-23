import { createHash } from "node:crypto";

import { EngineError } from "../errors";

export const FRAMING_ENGINE_VERSION = "framing-engine-v0.1";
export const PROJECT_FRAME_VERSION = "project-frame-v0.1";

export type WorkMode = "auto" | "explicit" | "none";
export type Establishment =
  | "declared"
  | "corroborated"
  | "inferred"
  | "conflicting"
  | "stale"
  | "unknown";
export type FrameControl = "hard" | "soft" | "query_only";

export type ProjectFrame = {
  version: string;
  digest: string;
  workTypes: string[];
  objective: string;
  constraints: string[];
  evidencePreferences: string[];
  source: string;
};

export type WorkSignal = {
  signalId: string;
  kind: string;
  text: string;
  observedAt: string;
};

export type WorkRequest = {
  mode: WorkMode;
  workType?: string;
  objective?: string;
  priorWorkType?: string;
  signals: WorkSignal[];
};

export type FrameResult = {
  applied: boolean;
  reason: string;
  control?: FrameControl;
  controlReason?: string;
  establishment?: {
    establishment: Establishment;
    workType: string | null;
    objective: string;
    supportingRefs: string[];
    conflictingRefs: string[];
    reasons: string[];
    source: string;
    priorWorkType: string | null;
  };
  projectFrameVersion?: string | null;
  projectFrameDigest?: string | null;
  excluded: Array<{ sourceId: string; reason: string }>;
};

export function digestProjectFrame(canonical: Omit<ProjectFrame, "digest">): string {
  return createHash("sha256")
    .update(JSON.stringify([canonical.version, [...canonical.workTypes].sort(), canonical.objective, [...canonical.constraints].sort()]), "utf8")
    .digest("hex")
    .slice(0, 16);
}

export function validateProjectFrame(raw: unknown, source: string): ProjectFrame {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new EngineError("FRAME_INVALID", `project frame at ${source} must be an object.`);
  }
  const r = raw as Record<string, unknown>;
  if (typeof r["objective"] !== "string" || !(r["objective"] as string).trim()) {
    throw new EngineError("FRAME_INVALID", `project frame at ${source} requires a non-empty objective.`);
  }
  const workTypes = Array.isArray(r["work_types"])
    ? (r["work_types"] as unknown[]).map(String)
    : Array.isArray(r["workTypes"])
      ? (r["workTypes"] as unknown[]).map(String)
      : [];
  if (workTypes.some((w) => !w.trim())) {
    throw new EngineError("FRAME_INVALID", `project frame at ${source} has an empty work type.`);
  }
  const canonical = {
    version: typeof r["version"] === "string" ? (r["version"] as string) : PROJECT_FRAME_VERSION,
    workTypes,
    objective: (r["objective"] as string).trim(),
    constraints: Array.isArray(r["constraints"]) ? (r["constraints"] as unknown[]).map(String) : [],
    evidencePreferences: Array.isArray(r["evidence_preferences"]) ? (r["evidence_preferences"] as unknown[]).map(String) : [],
    source,
  };
  return { ...canonical, digest: digestProjectFrame(canonical) };
}

export function normalizeWorkRequest(raw: {
  mode?: string;
  work_type?: string;
  objective?: string;
  prior_work_type?: string;
  signals?: Array<{ signal_id?: string; kind?: string; text?: string; observed_at?: string }>;
}): WorkRequest {
  const mode = (raw.mode ?? "auto").toLowerCase();
  if (mode !== "auto" && mode !== "explicit" && mode !== "none") {
    throw new EngineError("FRAME_INVALID", `unknown work mode ${JSON.stringify(raw.mode)}.`);
  }
  if (mode === "explicit" && !raw.work_type?.trim()) {
    throw new EngineError("FRAME_INVALID", "explicit work requires work_type.");
  }
  const signals: WorkSignal[] = (raw.signals ?? []).map((s, i) => ({
    signalId: s.signal_id ?? `signal-${i}`,
    kind: s.kind ?? "user",
    text: s.text ?? "",
    observedAt: s.observed_at ?? new Date(0).toISOString(),
  }));
  return {
    mode: mode as WorkMode,
    workType: raw.work_type,
    objective: raw.objective,
    priorWorkType: raw.prior_work_type,
    signals,
  };
}
