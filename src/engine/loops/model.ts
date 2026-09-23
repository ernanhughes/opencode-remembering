import { EngineError } from "../errors";
import { parseInstant } from "../temporal/model";

export const LOOP_ENGINE_VERSION = "loops-engine-v0.1";
export const LOOP_EVENT_SCHEMA = "loop-event-v0.1";
export const LOOP_REDUCER_VERSION = "loop-reducer-v0.1";
export const LOOP_CLOSURE_VERSION = "loop-closure-v0.1";
export const LOOP_STORE_VERSION = "loops-store-v0.1";

export type LoopState = "open" | "completed" | "cancelled" | "superseded" | "uncertain";
export type LoopTransitionKind = "TASK" | "DECISION" | "INVESTIGATION" | "REVIEW";
export type LoopEventKind = "create" | "transition" | "evidence";

export type ClosureCondition = {
  kind: string;
  ref?: string;
  satisfied: boolean;
};

export type LoopEvent = {
  eventId: string;
  loopId: string;
  kind: LoopEventKind;
  subject: string;
  transitionKind: LoopTransitionKind;
  fromState?: LoopState;
  toState?: LoopState;
  expected?: Record<string, unknown>;
  evidenceRefs: string[];
  closure: ClosureCondition[];
  reason: string;
  at: string;
};

export type LoopView = {
  loopId: string;
  subject: string;
  transitionKind: string;
  state: LoopState;
  reason: string;
  expected: Record<string, unknown>;
  evidenceRefs: string[];
  closure: Record<string, unknown>;
  history: LoopEvent[];
  createdAt: string;
  resolvedAt: string;
  searchComplete: boolean;
};

const STATES: LoopState[] = ["open", "completed", "cancelled", "superseded", "uncertain"];
const TERMINAL: LoopState[] = ["completed", "cancelled", "superseded"];

const ALLOWED: Record<LoopState, LoopState[]> = {
  open: ["completed", "cancelled", "superseded", "uncertain"],
  uncertain: ["open", "completed", "cancelled", "superseded"],
  completed: [],
  cancelled: [],
  superseded: [],
};

export function validateLoopEvent(raw: unknown): LoopEvent {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new EngineError("LOOP_INVALID", "loop event must be an object.");
  }
  const r = raw as Record<string, unknown>;
  for (const field of ["event_id", "loop_id", "subject"]) {
    if (typeof r[field] !== "string" || !(r[field] as string).trim()) {
      throw new EngineError("LOOP_INVALID", `loop event requires ${field}.`);
    }
  }
  const kind = r["kind"];
  if (kind !== "create" && kind !== "transition" && kind !== "evidence") {
    throw new EngineError("LOOP_INVALID", "loop event kind must be create, transition or evidence.");
  }
  const transitionKind = (r["transition_kind"] ?? r["transitionKind"] ?? "TASK") as string;
  if (!["TASK", "DECISION", "INVESTIGATION", "REVIEW"].includes(transitionKind)) {
    throw new EngineError("LOOP_INVALID", `unknown loop transition kind ${JSON.stringify(transitionKind)}.`);
  }
  const toState = (r["to_state"] ?? r["toState"]) as string | undefined;
  if (toState !== undefined && !(STATES as string[]).includes(toState)) {
    throw new EngineError("LOOP_INVALID", `unknown loop state ${JSON.stringify(toState)}.`);
  }
  if (typeof r["at"] !== "string") throw new EngineError("LOOP_INVALID", "loop event requires at.");
  parseInstant(r["at"] as string, "at");
  return {
    eventId: (r["event_id"] as string).trim(),
    loopId: (r["loop_id"] as string).trim(),
    kind: kind as LoopEventKind,
    subject: (r["subject"] as string).trim(),
    transitionKind: transitionKind as LoopTransitionKind,
    fromState: r["from_state"] as LoopState | undefined,
    toState: toState as LoopState | undefined,
    expected: (r["expected"] as Record<string, unknown> | undefined) ?? {},
    evidenceRefs: Array.isArray(r["evidence_refs"]) ? (r["evidence_refs"] as unknown[]).map(String) : [],
    closure: Array.isArray(r["closure"]) ? (r["closure"] as ClosureCondition[]) : [],
    reason: typeof r["reason"] === "string" ? (r["reason"] as string) : "",
    at: r["at"] as string,
  };
}

/** Pure reducer: append-only events -> derived loop views. Deterministic. */
export function reduceLoopEvents(events: LoopEvent[]): Map<string, LoopView> {
  const ordered = [...events].sort((a, b) => {
    const d = parseInstant(a.at, "at") - parseInstant(b.at, "at");
    if (d !== 0) return d;
    return a.eventId < b.eventId ? -1 : 1;
  });
  const byLoop = new Map<string, LoopEvent[]>();
  for (const e of ordered) {
    const list = byLoop.get(e.loopId) ?? [];
    list.push(e);
    byLoop.set(e.loopId, list);
  }
  const views = new Map<string, LoopView>();
  for (const [loopId, list] of byLoop) {
    views.set(loopId, reduceOneLoop(loopId, list));
  }
  return views;
}

function reduceOneLoop(loopId: string, list: LoopEvent[]): LoopView {
  const first = list[0];
  if (!first || first.kind !== "create") {
    throw new EngineError("LOOP_INVALID", `loop ${JSON.stringify(loopId)} has no create event.`);
  }
  let state: LoopState = "open";
  let reason = first.reason || "created.";
  const evidence = new Set<string>(first.evidenceRefs);
  let expected: Record<string, unknown> = { ...(first.expected ?? {}) };
  let closure: ClosureCondition[] = [...first.closure];
  let resolvedAt = "";
  for (const e of list.slice(1)) {
    for (const ref of e.evidenceRefs) evidence.add(ref);
    if (e.kind === "evidence") continue;
    if (e.kind === "transition" && e.toState) {
      if (TERMINAL.includes(state)) {
        throw new EngineError("LOOP_INVALID", `loop ${JSON.stringify(loopId)} is terminal (${state}); refusing transition to ${e.toState}.`);
      }
      if (!ALLOWED[state].includes(e.toState)) {
        throw new EngineError("LOOP_INVALID", `invalid loop transition ${state} -> ${e.toState}.`);
      }
      if ((e.toState === "completed" || e.toState === "superseded") && !closureSatisfied(closure, evidence) && closure.length > 0) {
        state = "uncertain";
        reason = `closure incomplete for ${e.toState}; held at uncertain.`;
        resolvedAt = e.at;
        continue;
      }
      state = e.toState;
      reason = e.reason || `transitioned to ${state}.`;
      if (e.expected) expected = { ...e.expected };
      if (e.closure.length > 0) closure = [...e.closure];
      if (TERMINAL.includes(state) || state === "uncertain") resolvedAt = e.at;
    }
  }
  const closureRecord: Record<string, unknown> = {
    version: LOOP_CLOSURE_VERSION,
    conditions: closure,
    satisfied: closureSatisfied(closure, evidence),
  };
  return {
    loopId,
    subject: first.subject,
    transitionKind: first.transitionKind,
    state,
    reason,
    expected,
    evidenceRefs: [...evidence].sort(),
    closure: closureRecord,
    history: list,
    createdAt: first.at,
    resolvedAt,
    searchComplete: evidence.size > 0,
  };
}

function closureSatisfied(closure: ClosureCondition[], evidence: Set<string>): boolean {
  return closure.every((c) => c.satisfied || (c.ref !== undefined && evidence.has(c.ref)));
}
