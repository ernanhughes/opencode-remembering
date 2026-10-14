import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

import { captureSession } from "./sessions";

const fake = "not-a-real-key-abcdef0123456789";
const dirs: string[] = [];
async function project() {
  const dir = await mkdtemp(path.join(os.tmpdir(), "remembering-redaction-"));
  dirs.push(dir);
  return dir;
}
afterEach(async () => {
  await Promise.all(dirs.splice(0).map((d) => rm(d, { recursive: true, force: true })));
});

describe("session persistence secret boundary", () => {
  test("redacts tool input, result and transcript text before writing", async () => {
    const dir = await project();
    const result = await captureSession(dir, "remembering_test", "ses_secret_test", {
      messages: [{
        id: "message-1",
        role: "tool",
        text: "OPENCODE_API_KEY=" + fake,
        toolCalls: [{ state: { input: { headers: { authorization: "Bearer " + fake } } } }],
        toolResults: [{ output: "OPENCODE_API_KEY  " + fake }],
      }],
    });
    expect(result.recorded).toBe(1);
    const saved = await readFile(result.transcriptPath, "utf8");
    expect(saved).not.toContain(fake);
    expect(saved).toContain("[REDACTED]");
    const decoded = JSON.parse(saved);
    expect(decoded.events[0].id).toBe("message-1");
  });

  test("re-scrubs a preexisting transcript even when the incoming id is duplicate", async () => {
    const dir = await project();
    const folder = path.join(dir, ".remembering", "sessions");
    await mkdir(folder, { recursive: true });
    const location = path.join(folder, "ses_legacy.json");
    await writeFile(location, JSON.stringify({
      kind: "opencode-session-transcript",
      version: 1,
      session_id: "ses_legacy",
      events: [{ id: "legacy-message", role: "tool", text: "OPENCODE_API_KEY=" + fake }],
    }));
    const result = await captureSession(dir, "remembering_test", "ses_legacy", {
      messages: [{ id: "legacy-message", role: "tool", text: "same id" }],
    });
    expect(result.recorded).toBe(0);
    expect(result.duplicates).toBe(1);
    expect(await readFile(location, "utf8")).not.toContain(fake);
  });
});
