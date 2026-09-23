export const SELECT_POLICY_VERSION = "decisive-selection-v0.1";
export const BUDGET_VERSION = "context-budget-v0.1";
export const REDUNDANCY_VERSION = "redundancy-v0.1";
export const PROVENANCE_VERSION = "provenance-selection-v0.1";

export type SelectionMode = "decisive" | "full";

export type SelectionCandidate = {
  candidateId: string;
  sourceId: string;
  text: string;
  score: number;
  /** Terminal stage that admitted the candidate (e.g. trust verdict). */
  provenance: string;
};

export type SelectionSummary = {
  policyVersion: string;
  inputCount: number;
  selectedCount: number;
  droppedRedundant: number;
  droppedLowValue: number;
  droppedBudget: number;
  charsBefore: number;
  charsAfter: number;
  compressionRatio: number;
  budgetInsufficient: boolean;
};

export type SelectionResult = {
  selected: SelectionCandidate[];
  summary: SelectionSummary;
  dropped: Array<{ candidateId: string; reason: string }>;
};

export function fingerprint(text: string): string {
  return text.toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

/** Provenance-aware redundancy: exact-text duplicates collapse, first wins. */
export function dropRedundant(candidates: SelectionCandidate[]): {
  kept: SelectionCandidate[];
  dropped: Array<{ candidateId: string; reason: string }>;
} {
  const seen = new Set<string>();
  const kept: SelectionCandidate[] = [];
  const dropped: Array<{ candidateId: string; reason: string }> = [];
  for (const c of candidates) {
    const fp = fingerprint(c.text);
    if (seen.has(fp)) {
      dropped.push({ candidateId: c.candidateId, reason: "redundant: identical normalized text already selected." });
      continue;
    }
    seen.add(fp);
    kept.push(c);
  }
  return { kept, dropped };
}

const LOW_VALUE_MIN_CHARS = 24;

/** Decisive low-value rule: near-empty or score-less filler is dropped visibly. */
export function isLowValue(candidate: SelectionCandidate): boolean {
  return candidate.text.trim().length < LOW_VALUE_MIN_CHARS;
}
