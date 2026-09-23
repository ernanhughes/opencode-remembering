import { describe, expect, test } from "bun:test";

import { authorizeWrite, builtinWritePolicy } from "./policy";
import { WriteService } from "./service";
import { MemoryWriteStore } from "./store";

const FIXED_NOW = "2026-01-10T00:00:00.000Z";

function service() {
  return new WriteService(new MemoryWriteStore(), builtinWritePolicy(), () => FIXED_NOW);
}

describe("explicit memory writes", () => {
  test("remember creates lineage and authorizes", async () => {
    const out = await service().execute({ action: "remember", content: "OAuth2 decision.", role: "decision", callerScope: "team-a", origin: "opencode" });
    expect(out.ok).toBe(true);
    expect(out.recordId).toBeDefined();
    expect(out.record?.lineageRoot).toBe(out.recordId as string);
    expect(out.authorization?.verdict).toBe("allow");
  });

  test("idempotency key deduplicates", async () => {
    const svc = service();
    const first = await svc.execute({ action: "remember", content: "Same fact.", idempotencyKey: "k1" });
    const second = await svc.execute({ action: "remember", content: "Same fact.", idempotencyKey: "k1" });
    expect(second.duplicate).toBe(true);
    expect(second.actionId).toBe(first.actionId);
  });

  test("correct and supersede preserve lineage; original stays in history", async () => {
    const svc = new WriteService(new MemoryWriteStore(), builtinWritePolicy(), () => FIXED_NOW);
    const created = await svc.execute({ action: "remember", content: "v1" });
    const corrected = await svc.execute({ action: "correct", content: "v2", targetRecordId: created.recordId as string });
    expect(corrected.relation).toEqual({ type: "corrects", targetRecordId: created.recordId as string });
    expect(corrected.record?.lineageRoot).toBe(created.record?.lineageRoot);
  });

  test("retraction preserves the record; double retract and correct-after-retract fail", async () => {
    const store = new MemoryWriteStore();
    const svc = new WriteService(store, builtinWritePolicy(), () => FIXED_NOW);
    const created = await svc.execute({ action: "remember", content: "v1" });
    const retracted = await svc.execute({ action: "retract", targetRecordId: created.recordId as string, reason: "wrong" });
    expect(retracted.ok).toBe(true);
    expect((await store.getRecord(created.recordId as string))?.state).toBe("retracted");
    const again = await svc.execute({ action: "retract", targetRecordId: created.recordId as string });
    expect(again.ok).toBe(false);
    const correct = await svc.execute({ action: "correct", content: "v2", targetRecordId: created.recordId as string });
    expect(correct.ok).toBe(false);
  });

  test("authorization denies production_state without evidence and unknown roles", async () => {
    const svc = service();
    const denied = await svc.execute({ action: "remember", content: "prod secret", role: "production_state" });
    expect(denied.ok).toBe(false);
    expect(denied.code).toBe("MEMORY_WRITE_DENIED");
    const allowed = await svc.execute({ action: "remember", content: "prod secret", role: "production_state", evidenceRefs: ["decisions/adr-1.md"] });
    expect(allowed.ok).toBe(true);
    await expect(svc.execute({ action: "remember", content: "x", role: "bogus" as never })).rejects.toThrow();
  });

  test("missing target fails with NOT_FOUND; history is queryable", async () => {
    const store = new MemoryWriteStore();
    const svc = new WriteService(store, builtinWritePolicy(), () => FIXED_NOW);
    const missing = await svc.execute({ action: "supersede", content: "v2", targetRecordId: "rec-ghost" });
    expect(missing.code).toBe("MEMORY_TARGET_NOT_FOUND");
    const created = await svc.execute({ action: "remember", content: "v1" });
    await svc.execute({ action: "correct", content: "v2", targetRecordId: created.recordId as string });
    expect((await store.history(created.recordId as string)).length).toBe(2);
  });

  test("write policy authorizes scopes explicitly", () => {
    const auth = authorizeWrite(builtinWritePolicy(), { role: "ordinary", evidenceRefs: [], callerScope: "", origin: "opencode" });
    expect(auth.verdict).toBe("deny");
  });
});
