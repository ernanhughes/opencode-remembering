import { describe, expect, test } from "bun:test";

import { validateEnvelope } from "./model";
import { reduceTemporalLog, resolveAtStandpoint } from "./reducer";
import { MemoryTemporalStore } from "./store";
import { TemporalService } from "./service";

function envelope(overrides: Record<string, unknown> = {}) {
  return validateEnvelope(
    {
      event_id: "e1",
      subject: "auth",
      kind: "assert",
      value: "oauth2",
      event_time: "2026-01-01T00:00:00.000Z",
      received_at: "2026-01-02T00:00:00.000Z",
      ...overrides,
    },
    "2026-01-02T00:00:00.000Z",
  );
}

describe("temporal reducer", () => {
  test("empty history resolves unknown with incomplete flag", () => {
    const resolved = resolveAtStandpoint([], "auth", { mode: "current" });
    expect(resolved.status).toBe("unknown");
    expect(resolved.incompleteHistory).toBe(true);
  });

  test("current event resolves value", () => {
    const report = reduceTemporalLog([envelope()]);
    expect(report.subjects.get("auth")?.value).toBe("oauth2");
    expect(report.subjects.get("auth")?.status).toBe("current");
  });

  test("future-effective state is planned, not current", () => {
    const report = reduceTemporalLog([
      envelope({ event_id: "e1", value: "old" }),
      envelope({ event_id: "e2", value: "new", effective_from: "2027-01-01T00:00:00.000Z", event_time: "2026-06-01T00:00:00.000Z", received_at: "2026-06-02T00:00:00.000Z", known_time: "2026-06-02T00:00:00.000Z" }),
    ]);
    expect(report.subjects.get("auth")?.status).toBe("planned");
  });

  test("supersession preserves lineage; retraction preserves history", () => {
    const report = reduceTemporalLog([
      envelope({ event_id: "e1", value: "v1" }),
      envelope({ event_id: "e2", kind: "supersede", value: "v2", supersedes: "e1", event_time: "2026-02-01T00:00:00.000Z", received_at: "2026-02-02T00:00:00.000Z", known_time: "2026-02-02T00:00:00.000Z" }),
    ]);
    const subject = report.subjects.get("auth");
    expect(subject?.trajectory).toHaveLength(2);
    expect(subject?.trajectory[0]?.supersededBy).toBe("e2");
    const retracted = reduceTemporalLog([
      envelope({ event_id: "e1", value: "v1" }),
      envelope({ event_id: "e3", kind: "retract", value: null, event_time: "2026-03-01T00:00:00.000Z", received_at: "2026-03-02T00:00:00.000Z", known_time: "2026-03-02T00:00:00.000Z" }),
    ]);
    expect(retracted.subjects.get("auth")?.status).toBe("retracted");
    expect(retracted.subjects.get("auth")?.trajectory).toHaveLength(2);
  });

  test("valid_at / as_known / bitemporal standpoints", () => {
    const events = [
      envelope({ event_id: "e1", value: "v1" }),
      envelope({ event_id: "e2", value: "v2", event_time: "2026-05-01T00:00:00.000Z", received_at: "2026-05-10T00:00:00.000Z", known_time: "2026-05-10T00:00:00.000Z" }),
    ];
    const historical = resolveAtStandpoint(events, "auth", { mode: "valid_at", validAt: "2026-03-01T00:00:00.000Z" });
    expect(historical.value).toBe("v1");
    expect(historical.status).toBe("historical");
    const asKnown = resolveAtStandpoint(events, "auth", { mode: "as_known", knownAt: "2026-03-01T00:00:00.000Z" });
    expect(asKnown.value).toBe("v1");
    const bitemporal = resolveAtStandpoint(events, "auth", {
      mode: "bitemporal", validAt: "2026-03-01T00:00:00.000Z", knownAt: "2026-06-01T00:00:00.000Z",
    });
    expect(bitemporal.value).toBe("v1");
  });

  test("unknown references and causality violations are visible", () => {
    const report = reduceTemporalLog([
      envelope({ event_id: "e9", kind: "supersede", value: "v9", supersedes: "ghost", event_time: "2026-04-01T00:00:00.000Z", received_at: "2026-04-02T00:00:00.000Z", known_time: "2026-04-02T00:00:00.000Z" }),
      envelope({ event_id: "e10", subject: "other", value: "x", event_time: "2026-05-01T00:00:00.000Z", received_at: "2026-04-01T00:00:00.000Z", known_time: "2026-04-01T00:00:00.000Z" }),
    ]);
    expect(report.unknownReferences).toContain("ghost");
    expect(report.causalityViolations).toContain("e10");
  });

  test("conflicting event identity fails loudly; duplicates are idempotent", async () => {
    const store = new MemoryTemporalStore();
    await store.append(envelope());
    const dup = await store.append(envelope());
    expect(dup.duplicate).toBe(true);
    await expect(store.append(envelope({ value: "different" }))).rejects.toThrow(/TEMPORAL_CONFLICT/);
    expect(() => reduceTemporalLog([
      envelope(),
      { ...envelope(), value: "different" },
    ])).toThrow(/TEMPORAL_CONFLICT/);
  });

  test("invalid timestamps and transitions fail visibly", () => {
    expect(() => envelope({ event_time: "not-a-date" })).toThrow(/TEMPORAL_EVENT_INVALID/);
    expect(() => envelope({ kind: "retract", value: "oops" })).toThrow(/TEMPORAL_EVENT_INVALID/);
  });

  test("service import counts duplicates and failures locally", async () => {
    const service = new TemporalService(new MemoryTemporalStore());
    const summary = await service.importLines(
      [
        JSON.stringify({ event_id: "e1", subject: "s", kind: "assert", value: "v", event_time: "2026-01-01T00:00:00.000Z" }),
        JSON.stringify({ event_id: "e1", subject: "s", kind: "assert", value: "v", event_time: "2026-01-01T00:00:00.000Z" }),
        "not json",
        JSON.stringify({ event_id: "", subject: "s" }),
      ],
      "2026-01-02T00:00:00.000Z",
    );
    expect(summary.imported).toBe(1);
    expect(summary.duplicates).toBe(1);
    expect(summary.failed).toHaveLength(2);
  });
});
