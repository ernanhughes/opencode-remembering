import { describe, expect, test } from "bun:test";

import { createTrace, verifyTrace } from "./model";
import { MemoryTraceStore } from "./store";
import { TraceService } from "./service";

const POLICIES = {
  retrieval: "hybrid:none",
  temporal: "current",
  frame: "none",
  trust: "builtin-default-v0.1",
  trustDigest: "abc",
  selection: "decisive",
};

function candidate(id: string, terminalStage = "selected") {
  return {
    candidateId: id, sourceId: `src-${id}`, text: `text ${id} sufficiently long for tests`,
    score: 1, stages: { retrieved: { admitted: true, reason: "retrieved." } }, terminalStage,
  };
}

describe("context trace", () => {
  test("creation persists immutably; find filters work", async () => {
    const service = new TraceService(new MemoryTraceStore());
    const trace = await service.create({
      query: "why oauth?", route: "recall", candidates: [candidate("c1")],
      policies: POLICIES, budget: { maxChars: 1000, maxResults: 5 },
    });
    expect(trace.digest).toHaveLength(16);
    const found = await service.find({ route: "recall" });
    expect(found).toHaveLength(1);
    expect(await service.find({ route: "influence" })).toHaveLength(0);
    const byChunk = await service.find({ chunkId: "c1" });
    expect(byChunk).toHaveLength(1);
  });

  test("explain isolates one candidate; invalid ids fail", async () => {
    const service = new TraceService(new MemoryTraceStore());
    const trace = await service.create({
      query: "q", route: "recall", candidates: [candidate("c1", "denied")],
      policies: POLICIES, budget: { maxChars: 1000, maxResults: 5 },
    });
    const explanation = await service.explain(trace.traceId, "c1");
    expect(explanation["terminalStage"]).toBe("denied");
    await expect(service.explain(trace.traceId, "ghost")).rejects.toThrow(/TRACE_NOT_FOUND/);
    await expect(service.get("ghost")).rejects.toThrow(/TRACE_NOT_FOUND/);
  });

  test("verification detects tampering", async () => {
    const service = new TraceService(new MemoryTraceStore());
    const trace = await service.create({
      query: "q", route: "recall", candidates: [candidate("c1")],
      policies: POLICIES, budget: { maxChars: 1000, maxResults: 5 },
    });
    expect((await service.verify(trace.traceId)).valid).toBe(true);
    expect(verifyTrace({ ...trace, query: "mutated" }).valid).toBe(false);
  });

  test("replay never mutates the original; persisted replay links back", async () => {
    const service = new TraceService(new MemoryTraceStore());
    const original = await service.create({
      query: "q", route: "influence", candidates: [candidate("c1", "denied")],
      policies: POLICIES, budget: { maxChars: 1000, maxResults: 5 },
    });
    const { original: kept, replayed, persisted } = await service.replay(original.traceId, "trust", (list) =>
      list.map((c) => ({ ...c, terminalStage: "admitted" as const })),
      true, "stricter-trust",
    );
    expect(kept.candidates[0]?.terminalStage).toBe("denied");
    expect(replayed.candidates[0]?.terminalStage).toBe("admitted");
    expect(replayed.replayOf).toBe(original.traceId);
    expect(persisted).toBe(true);
    const dry = await service.replay(original.traceId, "selection", (list) => list, false, "same");
    expect(dry.persisted).toBe(false);
    expect((await service.find({})).length).toBe(2);
  });

  test("diff reports membership and stage changes", async () => {
    const service = new TraceService(new MemoryTraceStore());
    const a = await service.create({
      query: "q", route: "recall", candidates: [candidate("c1", "selected"), candidate("c2", "dropped")],
      policies: POLICIES, budget: { maxChars: 1000, maxResults: 5 },
    });
    const b = await service.create({
      query: "q", route: "recall", candidates: [candidate("c1", "dropped"), candidate("c3", "selected")],
      policies: { ...POLICIES, trust: "explicit-v1" }, budget: { maxChars: 1000, maxResults: 5 },
    });
    const diff = await service.diff(a.traceId, b.traceId);
    expect(diff["onlyInA"]).toEqual(["c2"]);
    expect(diff["onlyInB"]).toEqual(["c3"]);
    expect(diff["policyChanged"]).toBe(true);
  });
});
