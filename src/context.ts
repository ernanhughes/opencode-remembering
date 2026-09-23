export type CaptureRecord = {
  id?: string;
  role: string;
  text: string;
  agent?: string;
  model?: string;
  tool_calls?: unknown[];
  tool_results?: unknown[];
};

export type SessionFacts = {
  sessionId?: string;
  agent?: string;
  provider?: string;
  model?: string;
  messages: CaptureRecord[];
};

function textFrom(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map(textFrom).filter(Boolean).join("\n");
  }
  if (!value || typeof value !== "object") return "";

  const record = value as Record<string, unknown>;
  if (record.type === "text" && typeof record.text === "string") {
    return record.text;
  }
  if (typeof record.text === "string") return record.text;
  if (record.parts !== undefined) return textFrom(record.parts);
  if (record.content !== undefined) return textFrom(record.content);
  if (record.message !== undefined) return textFrom(record.message);
  return "";
}

function roleOf(message: unknown): string {
  if (!message || typeof message !== "object") return "";
  const record = message as Record<string, unknown>;
  if (typeof record.role === "string") return record.role;
  if (record.info && typeof record.info === "object") {
    const info = record.info as Record<string, unknown>;
    if (typeof info.role === "string") return info.role;
  }
  return "";
}

function stringField(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function idOf(message: unknown): string | undefined {
  if (!message || typeof message !== "object") return undefined;
  const record = message as Record<string, unknown>;
  for (const key of ["id", "messageID", "messageId"]) {
    const id = stringField(record[key]);
    if (id) return id;
  }
  if (record.info && typeof record.info === "object") {
    const info = record.info as Record<string, unknown>;
    for (const key of ["id", "messageID", "messageId"]) {
      const id = stringField(info[key]);
      if (id) return id;
    }
  }
  return undefined;
}

function partsOf(message: unknown): unknown[] {
  if (!message || typeof message !== "object") return [];
  const record = message as Record<string, unknown>;
  const candidates = [record.parts, record.content];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

/** Collect tool-call / tool-result parts without assuming exact shapes. */
function toolIO(parts: unknown[]): {
  tool_calls: unknown[];
  tool_results: unknown[];
} {
  const tool_calls: unknown[] = [];
  const tool_results: unknown[] = [];
  for (const part of parts) {
    if (!part || typeof part !== "object") continue;
    const record = part as Record<string, unknown>;
    const type = typeof record.type === "string" ? record.type : "";
    if (
      type.includes("tool-call") ||
      type.includes("tool_call") ||
      type === "tool" ||
      record.toolCallID !== undefined ||
      record.tool_call_id !== undefined
    ) {
      tool_calls.push(part);
    } else if (
      type.includes("tool-result") ||
      type.includes("tool_result") ||
      record.toolResult !== undefined ||
      record.output !== undefined
    ) {
      tool_results.push(part);
    }
  }
  return { tool_calls, tool_results };
}

export function latestUserText(messages: unknown[]): string {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (roleOf(message) !== "user") continue;
    const text = textFrom(message).trim();
    if (text) return text;
  }
  return "";
}

/** Gate trivial keystrokes so we never inject memory for noise. */
export function isMeaningfulQuery(query: string): boolean {
  const text = query.trim();
  if (text.length < 3) return false;
  return /[A-Za-z0-9]/.test(text);
}

function captureRecord(message: unknown): CaptureRecord | null {
  const role = roleOf(message);
  const text = textFrom(message);
  const parts = partsOf(message);
  const { tool_calls, tool_results } = toolIO(parts);
  if (!role && !text && tool_calls.length === 0 && tool_results.length === 0) {
    return null;
  }
  const record: CaptureRecord = { role: role || "unknown", text };
  const id = idOf(message);
  if (id) record.id = id;
  if (tool_calls.length > 0) record.tool_calls = tool_calls;
  if (tool_results.length > 0) record.tool_results = tool_results;
  return record;
}

export function extractCaptureMessages(messages: unknown[]): CaptureRecord[] {
  const out: CaptureRecord[] = [];
  for (const message of messages ?? []) {
    const record = captureRecord(message);
    if (record) out.push(record);
  }
  return out;
}

/**
 * Canonical session facts available through the OpenCode plugin API.
 * Everything here is recorded verbatim; no summaries are derived.
 */
export function extractSessionFacts(event: unknown): SessionFacts {
  if (!event || typeof event !== "object") return { messages: [] };
  const record = event as Record<string, unknown>;
  const sessionId = stringField(record.sessionID ?? record.sessionId);
  const agent = stringField(record.agent);
  let provider: string | undefined;
  let model: string | undefined;
  const modelRef = record.model;
  if (typeof modelRef === "string") {
    model = stringField(modelRef);
  } else if (modelRef && typeof modelRef === "object") {
    const ref = modelRef as Record<string, unknown>;
    provider = stringField(ref.providerID ?? ref.provider);
    model = stringField(ref.modelID ?? ref.model);
  }
  const messages = Array.isArray(record.messages)
    ? extractCaptureMessages(record.messages)
    : [];
  return { sessionId, agent, provider, model, messages };
}

export function renderInjectedMemory(
  traceId: string,
  schema: string,
  content: string,
  route: string,
  temporalMode = "current",
  frame: { workType?: string; establishment?: string; control?: string } = {},
  trustPolicy?: string,
): string {
  const frameAttrs =
    frame.workType !== undefined
      ? ` work_type="${frame.workType}"` +
        ` frame_establishment="${frame.establishment ?? "unknown"}"` +
        ` frame_control="${frame.control ?? "query_only"}"`
      : "";
  const trustAttr =
    trustPolicy !== undefined ? ` trust_policy="${trustPolicy}"` : "";
  return [
    `<project_memory trace_id="${traceId}" schema="${schema}" route="${route}" temporal_mode="${temporalMode}"${frameAttrs}${trustAttr}>`,
    "Selected project history from Project Memory. Source and chunk IDs " +
      "are provenance, not authority: retrieval found this evidence, and " +
      "later policy stages decide whether it may influence present action.",
    content,
    "</project_memory>",
  ].join("\n");
}
