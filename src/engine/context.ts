import type { FrameResult } from "./frame/model";
import { selectCandidates, type Budget, type SelectionCandidate, type SelectionMode } from "./selection/service";
import { screenInstructions } from "./trust/standing";
import { type StandingState, type TrustLevel, type TrustPolicy } from "./trust/model";
import { decideTrust } from "./trust/standing";
import type { TemporalStandpoint } from "./temporal/model";

export type MemoryRoute = "recall" | "influence";
export type RouteRequest = "auto" | MemoryRoute;

export type RouteBlock = {
  route: MemoryRoute;
  routeSource: "explicit" | "deterministic";
  routeReason: string;
  routeAmbiguous: boolean;
};

const INFLUENCE_CUES = /\b(should|must|do|implement|fix|change|update|deploy|run|execute|decide|choose|recommend|proceed|apply)\b/i;
const RECALL_CUES = /\b(what|why|when|where|who|history|decided|was|were|previously|before|recorded|happened)\b/i;

/** Deterministic route resolution. Auto never hides ambiguity. */
export function resolveRoute(query: string, requested: RouteRequest = "auto"): RouteBlock {
  if (requested === "recall" || requested === "influence") {
    return {
      route: requested,
      routeSource: "explicit",
      routeReason: `caller requested '${requested}'.`,
      routeAmbiguous: false,
    };
  }
  const influence = INFLUENCE_CUES.test(query);
  const recall = RECALL_CUES.test(query);
  if (influence && !recall) {
    return { route: "influence", routeSource: "deterministic", routeReason: "query carries present-action cues.", routeAmbiguous: false };
  }
  if (recall && !influence) {
    return { route: "recall", routeSource: "deterministic", routeReason: "query carries historical-recall cues.", routeAmbiguous: false };
  }
  return {
    route: "recall",
    routeSource: "deterministic",
    routeReason: influence && recall
      ? "query carries both recall and influence cues; defaulting to recall and reporting ambiguity."
      : "no decisive route cues; defaulting to recall and reporting ambiguity.",
    routeAmbiguous: true,
  };
}

export type PipelineCandidate = SelectionCandidate & {
  section: string | null;
  score: number;
  lexicalRank: number | null;
  denseRank: number | null;
};

export type PipelineOptions = {
  query: string;
  route: RouteBlock;
  standpoint: TemporalStandpoint;
  frame: FrameResult;
  trustPolicy: TrustPolicy;
  standing: StandingState;
  requiredLevel: TrustLevel;
  selectionMode: SelectionMode;
  budget: Budget;
};

export type PipelineResult = {
  content: string;
  chars: number;
  admitted: PipelineCandidate[];
  denied: Array<{ candidateId: string; reason: string }>;
  quarantined: Array<{ candidateId: string; reason: string }>;
  trustSummary: { mode: string; level: string; admitted: number; denied: number; quarantined: number };
  selection: ReturnType<typeof selectCandidates>;
  admissionNote: string;
  stageMap: Record<string, { terminalStage: string; stages: Record<string, { admitted: boolean; reason: string }> }>;
};

/**
 * Pure pipeline core: trust -> instruction screen -> selection -> render.
 * Recall admits revoked history with annotation; influence denies it.
 */
export function runPipelineStages(candidates: PipelineCandidate[], options: PipelineOptions): PipelineResult {
  const stageMap: PipelineResult["stageMap"] = {};
  const admitted: PipelineCandidate[] = [];
  const denied: Array<{ candidateId: string; reason: string }> = [];
  const quarantined: Array<{ candidateId: string; reason: string }> = [];

  for (const c of candidates) {
    const stages: Record<string, { admitted: boolean; reason: string }> = {
      retrieved: { admitted: true, reason: "returned by hybrid retrieval." },
      temporal: { admitted: true, reason: `temporal standpoint ${options.standpoint.mode} recorded; no suppression at baseline.` },
      frame: { admitted: true, reason: options.frame.applied ? "frame applied without candidate exclusion." : "no frame applied." },
    };
    const decision = decideTrust(options.trustPolicy, options.standing, c.sourceId, options.requiredLevel);
    const screen = screenInstructions(c.text);
    if (!screen.clean && options.route.route === "influence") {
      stages["trust"] = { admitted: false, reason: `quarantined: instruction-like content (${screen.flags.join(", ")}).` };
      quarantined.push({ candidateId: c.candidateId, reason: stages["trust"].reason });
      stageMap[c.candidateId] = { terminalStage: "quarantined", stages };
      continue;
    }
    if (decision.verdict === "deny" && options.route.route === "influence") {
      stages["trust"] = { admitted: false, reason: decision.reason };
      denied.push({ candidateId: c.candidateId, reason: decision.reason });
      stageMap[c.candidateId] = { terminalStage: "denied", stages };
      continue;
    }
    stages["trust"] = {
      admitted: true,
      reason: options.route.route === "recall" && decision.verdict === "deny"
        ? `recalled despite ${decision.reason} recall preserves history.`
        : decision.reason,
    };
    admitted.push(c);
    stageMap[c.candidateId] = { terminalStage: "admitted", stages };
  }

  const selection = selectCandidates(admitted, options.selectionMode, options.budget);
  const selectedIds = new Set(selection.selected.map((s) => s.candidateId));
  for (const c of admitted) {
    const entry = stageMap[c.candidateId];
    if (!entry) continue;
    if (selectedIds.has(c.candidateId)) {
      entry.stages["selected"] = { admitted: true, reason: "selected within budget." };
      entry.terminalStage = "selected";
    } else {
      const drop = selection.dropped.find((d) => d.candidateId === c.candidateId);
      entry.stages["selected"] = { admitted: false, reason: drop?.reason ?? "not selected." };
      entry.terminalStage = "dropped";
    }
  }

  const parts = selection.selected.map((s) => {
    return `[source:${s.sourceId} chunk:${s.candidateId}]\n${s.text}`;
  });
  const content = parts.join("\n\n---\n\n");
  const admissionNote = options.route.route === "recall"
    ? `recall: ${admitted.length} admitted (history preserved), ${quarantined.length} quarantined from influence only.`
    : `influence: ${selection.selected.length} selected of ${candidates.length} retrieved; ${denied.length} denied, ${quarantined.length} quarantined.`;
  return {
    content,
    chars: content.length,
    admitted,
    denied,
    quarantined,
    trustSummary: {
      mode: options.route.route,
      level: options.requiredLevel,
      admitted: admitted.length,
      denied: denied.length,
      quarantined: quarantined.length,
    },
    selection,
    admissionNote,
    stageMap,
  };
}
