import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS = "1";

import { readJsonLines, appendJsonLine } from "./io";
import { jsonPaths, JsonWriteStore, JsonBaselineStore } from "./stores";
import { WriteService } from "../../write/service";
import { builtinWritePolicy } from "../../write/policy";
import type { MemoryActionRecord, MemoryRecord } from "../../write/model";

async function tempDir(): Promise<string> {
  return fs.mkdtemp(path.join(os.tmpdir(), "remembering-durability-"));
}

function record(id: string): MemoryRecord {
  return {
    recordId: id, content: `content of ${id}`, role: "ordinary",
    sourceId: `memory://${id}`, lineageRoot: id, standingCeiling: "FULL",
    state: "active", callerScope: "test", origin: "cli", evidenceRefs: [],
    eventTime: new Date().toISOString(), effectiveFrom: new Date().toISOString(),
    createdAt: new Date().toISOString(),
  };
}

function action(id: string, recordId: string | null): MemoryActionRecord {
  return {
    actionId: id, action: "remember", recordId, targetRecordId: null,
    reason: "", callerScope: "test", origin: "cli",
    idempotencyKey: null, relation: null, createdAt: new Date().toISOString(),
  };
}

describe("JSONL crash tolerance", () => {
  let dir: string;
  beforeEach(async () => { dir = await tempDir(); });
  afterEach(async () => { await fs.rm(dir, { recursive: true, force: true }); });

  test("a torn trailing line is quarantined; the intact prefix survives", async () => {
    const file = path.join(dir, "events.jsonl");
    await appendJsonLine(file, { eventId: "a" });
    await appendJsonLine(file, { eventId: "b" });
    // Simulate a process dying mid-append: partial bytes, no trailing newline.
    await fs.appendFile(file, `{"eventId":"c","subje`, "utf8");
    const rows = await readJsonLines<{ eventId: string }>(file);
    expect(rows.map((r) => r.eventId)).toEqual(["a", "b"]);
    const torn = await fs.readFile(`${file}.torn`, "utf8");
    expect(torn).toContain(`"subje`);
  });

  test("mid-file corruption still fails loudly", async () => {
    const file = path.join(dir, "events.jsonl");
    await appendJsonLine(file, { eventId: "a" });
    await fs.appendFile(file, "not json at all\n", "utf8");
    await appendJsonLine(file, { eventId: "b" });
    await expect(readJsonLines(file)).rejects.toThrow("corrupt JSONL");
  });
});

describe("JSON write generation commits", () => {
  let dir: string;
  beforeEach(async () => { dir = await tempDir(); });
  afterEach(async () => { await fs.rm(dir, { recursive: true, force: true }); });

  test("committed generations survive; failed transactions leave no trace", async () => {
    const store = new JsonWriteStore(dir, ".remembering/store");
    await store.initialise();
    await store.transaction(async () => {
      await store.putRecord(record("rec-1"));
      await store.putAction(action("act-1", "rec-1"));
    });
    expect((await store.counts()).records).toBe(1);
    // A new store instance reads the committed generation (manifest flip).
    const reopened = new JsonWriteStore(dir, ".remembering/store");
    expect(await reopened.getRecord("rec-1")).not.toBeNull();
    // Failed transaction: manifest still points at the old generation.
    await expect(
      reopened.transaction(async () => {
        await reopened.putRecord(record("rec-2"));
        throw new Error("simulated failure after record write");
      }),
    ).rejects.toThrow("simulated failure");
    expect(await reopened.getRecord("rec-2")).toBeNull();
    expect((await reopened.counts()).records).toBe(1);
  });

  test("orphan staging dirs from a crashed transaction are swept visibly", async () => {
    const store = new JsonWriteStore(dir, ".remembering/store");
    await store.initialise();
    await store.transaction(async () => {
      await store.putRecord(record("rec-1"));
    });
    // Simulate a crash: staging dir left behind, manifest untouched.
    const genRoot = path.join(jsonPaths(dir, ".remembering/store").dir, "write-generations");
    await fs.mkdir(path.join(genRoot, "staging-99999-orphan"), { recursive: true });
    await fs.writeFile(path.join(genRoot, "staging-99999-orphan", "records.jsonl"), "junk\n", "utf8");
    const sweep = new JsonWriteStore(dir, ".remembering/store");
    await sweep.initialise();
    expect(await sweep.getRecord("rec-1")).not.toBeNull();
    const recovery = await fs.readFile(
      path.join(jsonPaths(dir, ".remembering/store").dir, "recovery.jsonl"), "utf8",
    );
    expect(recovery).toContain("orphan-staging-swept");
    expect(await fs.rm(path.join(genRoot, "staging-99999-orphan"), { recursive: true, force: true }).then(() => "gone").catch(() => "gone")).toBe("gone");
  });

  test("failed explicit write leaves no partial record or action", async () => {
    const store = new JsonWriteStore(dir, ".remembering/store");
    await store.initialise();
    const policy = builtinWritePolicy();
    const failing = new WriteService(store, policy, () => new Date().toISOString(), async () => {
      throw new Error("index boom");
    });
    await expect(
      store.transaction(() => failing.execute({ action: "remember", content: "hello", callerScope: "test", origin: "cli" })),
    ).rejects.toThrow("index boom");
    const counts = await store.counts();
    expect(counts.records).toBe(0);
    expect(counts.actions).toBe(0);
  });

  test("stats surface torn quarantine and recovery diagnostics", async () => {
    const baseline = new JsonBaselineStore(dir, "remembering_diag", dir.toLowerCase(), "local:diag", ".remembering/store");
    await baseline.initialise(8);
    const stats = await baseline.stats();
    expect(stats["torn_quarantined_bytes"]).toBe(0);
    expect(stats["recovery_events"]).toBe(0);
  });
});
