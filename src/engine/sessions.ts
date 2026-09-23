import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import * as path from "node:path";

import { EngineError } from "./errors";

export const SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

export type SessionMessage = {
  id?: string;
  role: string;
  text: string;
  agent?: string;
  model?: string;
  toolCalls?: unknown[];
  toolResults?: unknown[];
  observedAt?: string;
};

export type CaptureResult = {
  ok: boolean;
  schema: string;
  sessionId: string;
  transcriptPath: string;
  recorded: number;
  duplicates: number;
  total: number;
};

function fingerprint(message: SessionMessage): string {
  const material = JSON.stringify({
    role: message.role ?? "",
    text: message.text ?? "",
    tools: [message.toolCalls ?? null, message.toolResults ?? null],
  });
  return `msg:${createHash("sha256").update(material, "utf8").digest("hex").slice(0, 16)}`;
}

/**
 * File-based session capture. Needs no database: canonical history must
 * survive even when PostgreSQL is down. Transcripts are append-only and
 * deduplicated by message id (or content fingerprint); existing events
 * are never rewritten.
 */
export async function captureSession(
  projectDirectory: string,
  schema: string,
  sessionId: string,
  facts: { agent?: string; provider?: string; model?: string; messages: SessionMessage[] },
): Promise<CaptureResult> {
  if (!SESSION_ID_PATTERN.test(sessionId)) {
    throw new EngineError(
      "CONFIG_INVALID",
      "session_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,127}; refusing to write outside the sessions directory.",
    );
  }
  if (!Array.isArray(facts.messages)) {
    throw new EngineError("CONFIG_INVALID", "messages must be a list.");
  }
  let root: string;
  try {
    const { realpath } = await import("node:fs/promises");
    root = await realpath(projectDirectory);
  } catch {
    root = projectDirectory;
  }
  const dir = path.join(root, ".remembering", "sessions");
  await mkdir(dir, { recursive: true });
  const transcriptPath = path.join(dir, `${sessionId}.json`);
  let existing: {
    kind: string;
    version: number;
    session_id: string;
    agent?: string;
    provider?: string;
    model?: string;
    events: Array<Record<string, unknown>>;
  };
  try {
    const raw = await readFile(transcriptPath, "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || !Array.isArray((parsed as { events?: unknown }).events)) {
      throw new EngineError(
        "CAPTURE_CONFLICT",
        `existing transcript is not valid JSON with an events list; refusing to overwrite canonical history.`,
      );
    }
    existing = parsed as typeof existing;
  } catch (error) {
    if (error instanceof EngineError) throw error;
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new EngineError(
        "CAPTURE_CONFLICT",
        `existing transcript is unreadable; refusing to overwrite canonical history.`,
      );
    }
    existing = {
      kind: "opencode-session-transcript",
      version: 1,
      session_id: sessionId,
      agent: facts.agent,
      provider: facts.provider,
      model: facts.model,
      events: [],
    };
  }
  const known = new Set<string>();
  for (const event of existing.events) {
    const id = typeof event["id"] === "string" ? (event["id"] as string) : fingerprint(event as unknown as SessionMessage);
    known.add(id);
  }
  let recorded = 0;
  let duplicates = 0;
  for (const message of facts.messages) {
    if (!message.text?.trim() && !message.toolCalls?.length && !message.toolResults?.length) {
      duplicates += 1;
      continue;
    }
    const id = message.id ?? fingerprint(message);
    if (known.has(id)) {
      duplicates += 1;
      continue;
    }
    known.add(id);
    existing.events.push({
      id,
      role: message.role,
      text: message.text,
      agent: message.agent ?? facts.agent ?? null,
      model: message.model ?? facts.model ?? null,
      tool_calls: message.toolCalls ?? [],
      tool_results: message.toolResults ?? [],
      observed_at: message.observedAt ?? new Date().toISOString(),
    });
    recorded += 1;
  }
  await writeFile(transcriptPath, JSON.stringify(existing, null, 2), "utf8");
  return {
    ok: true,
    schema,
    sessionId,
    transcriptPath,
    recorded,
    duplicates,
    total: existing.events.length,
  };
}
