import { readFile } from "node:fs/promises";
import * as path from "node:path";

import { EngineError } from "../errors";
import {
  FRAMING_ENGINE_VERSION,
  normalizeWorkRequest,
  validateProjectFrame,
  type FrameResult,
  type ProjectFrame,
} from "./model";

export type FrameHealth = {
  projectFramePresent: boolean;
  projectFrameValid: boolean;
  projectFrameVersion: string | null;
  projectFrameDigest: string | null;
  knownWorkTypes: string[];
  framingEngineVersion: string;
  frameError: string | null;
};

export async function loadProjectFrame(projectDirectory: string): Promise<{ frame: ProjectFrame | null; error: string | null }> {
  const file = path.join(projectDirectory, ".remembering", "project-frame.json");
  let content: string;
  try {
    content = await readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return { frame: null, error: null };
    throw error;
  }
  let raw: unknown;
  try {
    raw = JSON.parse(content);
  } catch {
    return { frame: null, error: `project frame at ${file} is not valid JSON.` };
  }
  try {
    return { frame: validateProjectFrame(raw, file), error: null };
  } catch (error) {
    return { frame: null, error: error instanceof Error ? error.message.slice(0, 300) : String(error).slice(0, 300) };
  }
}

/**
 * Deterministic frame establishment. No LLM calls.
 * Signals are bounded work hints, never a dump of canonical history.
 */
export function establishFrame(
  projectFrame: ProjectFrame | null,
  frameError: string | null,
  rawWork: { mode?: string; work_type?: string; objective?: string; prior_work_type?: string; signals?: Array<{ signal_id?: string; kind?: string; text?: string; observed_at?: string }> },
): FrameResult {
  const work = normalizeWorkRequest(rawWork);
  if (work.mode === "none") {
    return { applied: false, reason: "work mode 'none': framing disabled for this request.", excluded: [] };
  }
  if (frameError) {
    throw new EngineError("FRAME_INVALID", frameError);
  }
  if (!projectFrame) {
    return { applied: false, reason: "no project frame configured: framing disabled, retrieval unaffected.", excluded: [] };
  }
  if (work.mode === "explicit") {
    const workType = work.workType as string;
    const known = projectFrame.workTypes.includes(workType);
    return {
      applied: true,
      reason: `explicit work type ${JSON.stringify(workType)} declared by caller.`,
      control: known ? "hard" : "query_only",
      controlReason: known
        ? "declared work type is a known project work type."
        : "declared work type is unknown to the project frame: query-only.",
      establishment: {
        establishment: known ? "declared" : "unknown",
        workType,
        objective: work.objective ?? projectFrame.objective,
        supportingRefs: [],
        conflictingRefs: [],
        reasons: ["caller-declared work type"],
        source: "explicit",
        priorWorkType: work.priorWorkType ?? null,
      },
      projectFrameVersion: projectFrame.version,
      projectFrameDigest: projectFrame.digest,
      excluded: [],
    };
  }
  // auto: match bounded signals against known work types.
  const haystack = work.signals.map((s) => `${s.kind} ${s.text}`.toLowerCase()).join("\n");
  const hits = projectFrame.workTypes.filter((w) => haystack.includes(w.toLowerCase()));
  const prior = work.priorWorkType ?? null;
  if (hits.length === 1) {
    const workType = hits[0] as string;
    const corroborated = prior === null || prior === workType;
    return {
      applied: true,
      reason: `signal-matched work type ${JSON.stringify(workType)}.`,
      control: "soft",
      controlReason: "inferred work type assists ranking; it never erases evidence.",
      establishment: {
        establishment: corroborated ? "corroborated" : "inferred",
        workType,
        objective: work.objective ?? projectFrame.objective,
        supportingRefs: work.signals.filter((s) => s.text.toLowerCase().includes(workType.toLowerCase())).map((s) => s.signalId),
        conflictingRefs: [],
        reasons: corroborated ? ["signal match", "prior agreement"] : ["signal match"],
        source: "auto",
        priorWorkType: prior,
      },
      projectFrameVersion: projectFrame.version,
      projectFrameDigest: projectFrame.digest,
      excluded: [],
    };
  }
  if (hits.length > 1) {
    return {
      applied: true,
      reason: `conflicting signal matches: ${hits.join(", ")}.`,
      control: "query_only",
      controlReason: "conflicting work-type evidence: query-only until resolved.",
      establishment: {
        establishment: "conflicting",
        workType: null,
        objective: work.objective ?? projectFrame.objective,
        supportingRefs: [],
        conflictingRefs: hits,
        reasons: ["multiple work-type matches"],
        source: "auto",
        priorWorkType: prior,
      },
      projectFrameVersion: projectFrame.version,
      projectFrameDigest: projectFrame.digest,
      excluded: [],
    };
  }
  return {
    applied: true,
    reason: "insufficient work-type evidence: project objective applies without work-type control.",
    control: "query_only",
    controlReason: "unknown work type: query-only.",
    establishment: {
      establishment: "unknown",
      workType: null,
      objective: work.objective ?? projectFrame.objective,
      supportingRefs: [],
      conflictingRefs: [],
      reasons: ["no work-type match"],
      source: "auto",
      priorWorkType: prior,
    },
    projectFrameVersion: projectFrame.version,
    projectFrameDigest: projectFrame.digest,
    excluded: [],
  };
}

export async function frameHealth(projectDirectory: string): Promise<FrameHealth> {
  const { frame, error } = await loadProjectFrame(projectDirectory);
  return {
    projectFramePresent: frame !== null,
    projectFrameValid: error === null,
    projectFrameVersion: frame?.version ?? null,
    projectFrameDigest: frame?.digest ?? null,
    knownWorkTypes: frame?.workTypes ?? [],
    framingEngineVersion: FRAMING_ENGINE_VERSION,
    frameError: error,
  };
}
