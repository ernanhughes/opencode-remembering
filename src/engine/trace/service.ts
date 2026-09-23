import { EngineError } from "../errors";
import { createTrace, verifyTrace, type ContextTraceRecord } from "./model";
import { requireTrace, type TraceStore } from "./store";

export type ReplayKind = "trust" | "selection";

export class TraceService {
  constructor(private readonly store: TraceStore) {}

  async create(input: Parameters<typeof createTrace>[0]): Promise<ContextTraceRecord> {
    const trace = createTrace(input);
    await this.store.put(trace);
    return trace;
  }

  async get(traceId: string): Promise<ContextTraceRecord> {
    return requireTrace(await this.store.get(traceId), traceId);
  }

  async find(filters: Parameters<TraceStore["find"]>[0]): Promise<ContextTraceRecord[]> {
    return this.store.find(filters);
  }

  async explain(traceId: string, candidateId: string): Promise<Record<string, unknown>> {
    const trace = await this.get(traceId);
    const candidate = trace.candidates.find((c) => c.candidateId === candidateId);
    if (!candidate) throw new EngineError("TRACE_NOT_FOUND", `trace ${JSON.stringify(traceId)} has no candidate ${JSON.stringify(candidateId)}.`);
    return {
      traceId, candidateId,
      sourceId: candidate.sourceId,
      terminalStage: candidate.terminalStage,
      score: candidate.score,
      stages: candidate.stages,
      policies: trace.policies,
    };
  }

  async verify(traceId: string): Promise<{ valid: boolean; reason: string }> {
    const trace = await this.get(traceId);
    return verifyTrace(trace);
  }

  /**
   * Replay stored evidence under a different policy view.
   * Never mutates the original trace; optionally persists a new trace linked via replayOf.
   */
  async replay(
    traceId: string,
    kind: ReplayKind,
    apply: (candidates: ContextTraceRecord["candidates"]) => ContextTraceRecord["candidates"],
    persist: boolean,
    policyNote: string,
  ): Promise<{ original: ContextTraceRecord; replayed: ContextTraceRecord; persisted: boolean }> {
    if (kind !== "trust" && kind !== "selection") {
      throw new EngineError("TRACE_NOT_FOUND", `unknown replay kind ${JSON.stringify(kind)}.`);
    }
    const original = await this.get(traceId);
    const replayed = createTrace({
      query: original.query,
      route: original.route,
      workType: original.workType,
      candidates: apply(original.candidates.map((c) => ({ ...c, stages: { ...c.stages } }))),
      policies: { ...original.policies, [kind]: policyNote },
      budget: original.budget,
      replayOf: original.traceId,
    });
    if (persist) await this.store.put(replayed);
    return { original, replayed, persisted: persist };
  }

  async diff(traceId: string, diffWith: string): Promise<Record<string, unknown>> {
    const a = await this.get(traceId);
    const b = await this.get(diffWith);
    const aIds = new Set(a.candidates.map((c) => c.candidateId));
    const bIds = new Set(b.candidates.map((c) => c.candidateId));
    const onlyInA = [...aIds].filter((id) => !bIds.has(id));
    const onlyInB = [...bIds].filter((id) => !aIds.has(id));
    const stageChanges = a.candidates
      .filter((c) => bIds.has(c.candidateId))
      .map((c) => {
        const other = b.candidates.find((x) => x.candidateId === c.candidateId);
        return { candidateId: c.candidateId, from: c.terminalStage, to: other?.terminalStage ?? null };
      })
      .filter((d) => d.from !== d.to);
    return {
      traceId, diffWith, onlyInA, onlyInB, stageChanges,
      policyChanged: JSON.stringify(a.policies) !== JSON.stringify(b.policies),
    };
  }
}
