import { createHash } from "node:crypto";

import { EngineError } from "../errors";

export const TEMPORAL_STORE_VERSION = "temporal-store-v0.1";
export const TEMPORAL_EVENT_SCHEMA = "temporal-event-v0.1";
export const TEMPORAL_REDUCER_VERSION = "temporal-reducer-v0.1";

export type TemporalEventKind = "assert" | "correct" | "supersede" | "retract";
export type TemporalStandpointMode = "current" | "valid_at" | "as_known" | "bitemporal";

export type TemporalEvent = {
  eventId: string;
  subject: string;
  kind: TemporalEventKind;
  /** Null for retractions. */
  value: string | null;
  /** When the underlying fact held (ISO 8601). */
  eventTime: string;
  /** When the system learned it (ISO 8601). Defaults to receivedAt. */
  knownTime?: string;
  /** When it takes effect (ISO 8601). Defaults to eventTime. */
  effectiveFrom?: string;
  supersedes?: string;
  reason?: string;
};

export type TemporalEnvelope = TemporalEvent & { receivedAt: string };

export type TemporalStandpoint = {
  mode: TemporalStandpointMode;
  validAt?: string;
  knownAt?: string;
};

const KINDS: TemporalEventKind[] = ["assert", "correct", "supersede", "retract"];

export function parseInstant(value: string, field: string): number {
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", `${field} is not a valid ISO 8601 timestamp: ${JSON.stringify(value)}.`);
  }
  return ms;
}

export function validateTemporalEvent(raw: unknown): TemporalEvent {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", "temporal event must be an object.");
  }
  const r = raw as Record<string, unknown>;
  if (typeof r["event_id"] !== "string" || !(r["event_id"] as string).trim()) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", "temporal event requires a non-empty event_id.");
  }
  if (typeof r["subject"] !== "string" || !(r["subject"] as string).trim()) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", "temporal event requires a non-empty subject.");
  }
  if (typeof r["kind"] !== "string" || !(KINDS as string[]).includes(r["kind"] as string)) {
    throw new EngineError(
      "TEMPORAL_EVENT_INVALID",
      `temporal event kind must be one of ${KINDS.join(", ")}.`,
    );
  }
  const kind = r["kind"] as TemporalEventKind;
  if (kind === "retract") {
    if (r["value"] !== null && r["value"] !== undefined) {
      throw new EngineError("TEMPORAL_EVENT_INVALID", "retract events must carry value null.");
    }
  } else if (typeof r["value"] !== "string" || !(r["value"] as string).trim()) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", `temporal event kind '${kind}' requires a non-empty value.`);
  }
  if (typeof r["event_time"] !== "string") {
    throw new EngineError("TEMPORAL_EVENT_INVALID", "temporal event requires event_time.");
  }
  parseInstant(r["event_time"] as string, "event_time");
  if (r["known_time"] !== undefined) {
    if (typeof r["known_time"] !== "string") throw new EngineError("TEMPORAL_EVENT_INVALID", "known_time must be a string.");
    parseInstant(r["known_time"] as string, "known_time");
  }
  if (r["effective_from"] !== undefined) {
    if (typeof r["effective_from"] !== "string") throw new EngineError("TEMPORAL_EVENT_INVALID", "effective_from must be a string.");
    parseInstant(r["effective_from"] as string, "effective_from");
  }
  return {
    eventId: (r["event_id"] as string).trim(),
    subject: (r["subject"] as string).trim(),
    kind,
    value: kind === "retract" ? null : (r["value"] as string),
    eventTime: r["event_time"] as string,
    knownTime: r["known_time"] as string | undefined,
    effectiveFrom: r["effective_from"] as string | undefined,
    supersedes: typeof r["supersedes"] === "string" ? (r["supersedes"] as string) : undefined,
    reason: typeof r["reason"] === "string" ? (r["reason"] as string) : undefined,
  };
}

export function validateEnvelope(raw: unknown, fallbackReceivedAt: string): TemporalEnvelope {
  const event = validateTemporalEvent(raw);
  const r = raw as Record<string, unknown>;
  const receivedAt = typeof r["received_at"] === "string" && (r["received_at"] as string).trim()
    ? (r["received_at"] as string)
    : fallbackReceivedAt;
  parseInstant(receivedAt, "received_at");
  return { ...event, receivedAt };
}

export function envelopeDigest(envelope: TemporalEnvelope): string {
  return createHash("sha256")
    .update(JSON.stringify([envelope.eventId, envelope.subject, envelope.kind, envelope.value, envelope.eventTime, envelope.knownTime ?? null, envelope.effectiveFrom ?? null]), "utf8")
    .digest("hex")
    .slice(0, 16);
}

export function validateStandpoint(raw: { mode?: string; valid_at?: string; known_at?: string }): TemporalStandpoint {
  const mode = raw.mode ?? "current";
  if (mode !== "current" && mode !== "valid_at" && mode !== "as_known" && mode !== "bitemporal") {
    throw new EngineError("TEMPORAL_EVENT_INVALID", `unknown temporal mode ${JSON.stringify(mode)}.`);
  }
  if ((mode === "valid_at" || mode === "bitemporal") && !raw.valid_at?.trim()) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", `temporal mode '${mode}' requires valid_at.`);
  }
  if ((mode === "as_known" || mode === "bitemporal") && !raw.known_at?.trim()) {
    throw new EngineError("TEMPORAL_EVENT_INVALID", `temporal mode '${mode}' requires known_at.`);
  }
  if (raw.valid_at !== undefined) parseInstant(raw.valid_at, "valid_at");
  if (raw.known_at !== undefined) parseInstant(raw.known_at, "known_at");
  return { mode, validAt: raw.valid_at, knownAt: raw.known_at };
}
