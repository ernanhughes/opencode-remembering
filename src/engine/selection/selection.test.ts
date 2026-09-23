import { describe, expect, test } from "bun:test";

import { selectCandidates, type Budget } from "./service";
import type { SelectionCandidate } from "./model";

function candidate(id: string, text: string, score = 1): SelectionCandidate {
  return { candidateId: id, sourceId: `src-${id}`, text, score, provenance: "retrieval:fused" };
}

const BUDGET: Budget = { maxChars: 200, maxResults: 3 };

describe("decisive selection", () => {
  test("empty set selects nothing", () => {
    const result = selectCandidates([], "decisive", BUDGET);
    expect(result.selected).toEqual([]);
    expect(result.summary.selectedCount).toBe(0);
  });

  test("decisive drops low-value and redundant evidence visibly", () => {
    const result = selectCandidates(
      [candidate("a", "The authentication design uses OAuth2 with rotating secrets.", 0.9), candidate("b", "The authentication design uses OAuth2 with rotating secrets.", 0.8), candidate("c", "x", 0.7)],
      "decisive",
      BUDGET,
    );
    expect(result.selected.map((s) => s.candidateId)).toEqual(["a"]);
    expect(result.summary.droppedRedundant).toBe(1);
    expect(result.summary.droppedLowValue).toBe(1);
  });

  test("full mode keeps duplicates but still budgets", () => {
    const text = "The authentication design uses OAuth2 with rotating secrets.";
    const result = selectCandidates([candidate("a", text), candidate("b", text)], "full", BUDGET);
    expect(result.selected).toHaveLength(2);
    expect(result.summary.droppedRedundant).toBe(0);
  });

  test("budget truncation is observable", () => {
    const long = "word ".repeat(100);
    const result = selectCandidates([candidate("a", "short but sufficiently long text here.", 1), candidate("b", long, 0.5)], "decisive", { maxChars: 60, maxResults: 5 });
    expect(result.selected.map((s) => s.candidateId)).toEqual(["a"]);
    expect(result.summary.droppedBudget).toBe(1);
    expect(result.summary.charsAfter).toBeLessThanOrEqual(60);
  });

  test("provenance is retained; same input is deterministic", () => {
    const input = [candidate("a", "alpha content sufficiently long.", 0.5), candidate("b", "beta content sufficiently long here.", 0.9)];
    const first = selectCandidates(input, "decisive", BUDGET);
    const second = selectCandidates(input, "decisive", BUDGET);
    expect(first.selected[0]?.provenance).toBe("retrieval:fused");
    expect(first.selected.map((s) => s.candidateId)).toEqual(second.selected.map((s) => s.candidateId));
    // Input order is preserved (score order in the pipeline), not re-sorted.
    expect(first.selected.map((s) => s.candidateId)).toEqual(["a", "b"]);
  });
});
