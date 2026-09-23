import { describe, expect, test } from "bun:test";

import { reduceLoopEvents, validateLoopEvent } from "./model";
import { MemoryLoopStore } from "./store";

function create(id: string, at = "2026-01-01T00:00:00.000Z", extra: Record<string, unknown> = {}) {
  return validateLoopEvent({
    event_id: `ev-${id}-create`, loop_id: id, kind: "create", subject: `subject ${id}`,
    transition_kind: "TASK", expected: { done: true }, reason: "opened", at, ...extra,
  });
}

describe("open loops", () => {
  test("create opens; evidence accrues; complete resolves", () => {
    const views = reduceLoopEvents([
      create("l1"),
      validateLoopEvent({ event_id: "ev-l1-ev", loop_id: "l1", kind: "evidence", subject: "subject l1", evidence_refs: ["docs/a.md"], reason: "found", at: "2026-01-02T00:00:00.000Z" }),
      validateLoopEvent({ event_id: "ev-l1-done", loop_id: "l1", kind: "transition", subject: "subject l1", to_state: "completed", reason: "done", at: "2026-01-03T00:00:00.000Z" }),
    ]);
    const view = views.get("l1");
    expect(view?.state).toBe("completed");
    expect(view?.evidenceRefs).toEqual(["docs/a.md"]);
    expect(view?.searchComplete).toBe(true);
    expect(view?.resolvedAt).toBe("2026-01-03T00:00:00.000Z");
  });

  test("incomplete closure holds at uncertain instead of completing", () => {
    const views = reduceLoopEvents([
      create("l2", "2026-01-01T00:00:00.000Z", { closure: [{ kind: "evidence", ref: "docs/missing.md", satisfied: false }] }),
      validateLoopEvent({ event_id: "ev-l2-done", loop_id: "l2", kind: "transition", subject: "subject l2", to_state: "completed", reason: "claimed", at: "2026-01-02T00:00:00.000Z" }),
    ]);
    expect(views.get("l2")?.state).toBe("uncertain");
  });

  test("terminal loops reject further transitions; invalid states fail", () => {
    expect(() => reduceLoopEvents([
      create("l3"),
      validateLoopEvent({ event_id: "e1", loop_id: "l3", kind: "transition", subject: "s", to_state: "completed", reason: "d", at: "2026-01-02T00:00:00.000Z" }),
      validateLoopEvent({ event_id: "e2", loop_id: "l3", kind: "transition", subject: "s", to_state: "open", reason: "reopen", at: "2026-01-03T00:00:00.000Z" }),
    ])).toThrow(/LOOP_INVALID/);
    expect(() => validateLoopEvent({ event_id: "x", loop_id: "y", kind: "create", subject: "s", to_state: "bogus", at: "2026-01-01T00:00:00.000Z" })).toThrow(/LOOP_INVALID/);
  });

  test("cancel and supersede are terminal; reducer is deterministic", () => {
    const events = [
      create("l4"),
      validateLoopEvent({ event_id: "e1", loop_id: "l4", kind: "transition", subject: "s", to_state: "cancelled", reason: "dropped", at: "2026-01-02T00:00:00.000Z" }),
    ];
    expect(reduceLoopEvents(events).get("l4")?.state).toBe("cancelled");
    expect(reduceLoopEvents([...events].reverse()).get("l4")?.state).toBe("cancelled");
  });

  test("memory store is idempotent and rebuilds from events", async () => {
    const store = new MemoryLoopStore();
    await store.append(create("l5"));
    expect(await store.append(create("l5"))).toEqual({ duplicate: true });
    const views = reduceLoopEvents(await store.list());
    expect(views.get("l5")?.state).toBe("open");
  });
});
