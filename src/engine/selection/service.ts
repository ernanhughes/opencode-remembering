import {
  BUDGET_VERSION,
  dropRedundant,
  isLowValue,
  PROVENANCE_VERSION,
  REDUNDANCY_VERSION,
  SELECT_POLICY_VERSION,
  type SelectionCandidate,
  type SelectionMode,
  type SelectionResult,
} from "./model";

export type Budget = { maxChars: number; maxResults: number };

/**
 * Deterministic selector. Input order is score order; output preserves it.
 * Every exclusion is reported; nothing is silently discarded.
 */
export function selectCandidates(
  candidates: SelectionCandidate[],
  mode: SelectionMode,
  budget: Budget,
): SelectionResult {
  const charsBefore = candidates.reduce((acc, c) => acc + c.text.length, 0);
  const dropped: Array<{ candidateId: string; reason: string }> = [];
  let working = [...candidates];
  let droppedRedundant = 0;
  let droppedLowValue = 0;

  if (mode === "decisive") {
    const lowKept: SelectionCandidate[] = [];
    for (const c of working) {
      if (isLowValue(c)) {
        dropped.push({ candidateId: c.candidateId, reason: "low-value: below minimum informative length." });
        droppedLowValue += 1;
      } else {
        lowKept.push(c);
      }
    }
    working = lowKept;
    const { kept, dropped: dupes } = dropRedundant(working);
    working = kept;
    dropped.push(...dupes);
    droppedRedundant = dupes.length;
  }

  const selected: SelectionCandidate[] = [];
  let chars = 0;
  let droppedBudget = 0;
  for (const c of working) {
    if (selected.length >= budget.maxResults) {
      dropped.push({ candidateId: c.candidateId, reason: "budget: max results reached." });
      droppedBudget += 1;
      continue;
    }
    if (chars + c.text.length > budget.maxChars && selected.length > 0) {
      dropped.push({ candidateId: c.candidateId, reason: "budget: max chars reached." });
      droppedBudget += 1;
      continue;
    }
    selected.push(c);
    chars += c.text.length;
  }
  const charsAfter = chars;
  return {
    selected,
    dropped,
    summary: {
      policyVersion: SELECT_POLICY_VERSION,
      inputCount: candidates.length,
      selectedCount: selected.length,
      droppedRedundant,
      droppedLowValue,
      droppedBudget,
      charsBefore,
      charsAfter,
      compressionRatio: charsBefore === 0 ? 1 : Math.round((charsAfter / charsBefore) * 1000) / 1000,
      budgetInsufficient: mode === "decisive" && working.length > selected.length && selected.length === 0,
    },
  };
}

export { BUDGET_VERSION, PROVENANCE_VERSION, REDUNDANCY_VERSION, SELECT_POLICY_VERSION };
export type { SelectionCandidate, SelectionMode };
