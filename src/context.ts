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

export function latestUserText(messages: unknown[]): string {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (roleOf(message) !== "user") continue;
    const text = textFrom(message).trim();
    if (text) return text;
  }
  return "";
}

export function renderInjectedMemory(
  traceId: string,
  schema: string,
  content: string,
): string {
  return [
    `<project_memory trace_id="${traceId}" schema="${schema}">`,
    "This is selected project history from Project Memory. Treat source IDs as provenance.",
    content,
    "</project_memory>",
  ].join("\n");
}
