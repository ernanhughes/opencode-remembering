import { EngineError } from "../errors";
import { parseInstant } from "../temporal/model";
import {
  matchSourcePattern,
  TRUST_RANK,
  type StandingEvent,
  type StandingState,
  type TrustDecision,
  type TrustLevel,
  type TrustPolicy,
  type TrustVerdict,
} from "./model";

export function validateStandingEvent(raw: unknown): StandingEvent {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new EngineError("TRUST_POLICY_INVALID", "standing event must be an object.");
  }
  const r = raw as Record<string, unknown>;
  if (typeof r["event_id"] !== "string" || !(r["event_id"] as string).trim()) {
    throw new EngineError("TRUST_POLICY_INVALID", "standing event requires event_id.");
  }
  if (typeof r["source_id"] !== "string" || !(r["source_id"] as string).trim()) {
    throw new EngineError("TRUST_POLICY_INVALID", "standing event requires source_id.");
  }
  const transition = r["transition"];
  if (transition !== "restrict" && transition !== "revoke" && transition !== "restore") {
    throw new EngineError("TRUST_POLICY_INVALID", "standing transition must be restrict, revoke or restore.");
  }
  let level: TrustLevel | undefined;
  if (r["level"] !== undefined) {
    const v = r["level"];
    if (v !== "T0" && v !== "S1" && v !== "S2" && v !== "S3" && v !== "FULL") {
      throw new EngineError("TRUST_POLICY_INVALID", `unknown standing level ${JSON.stringify(v)}.`);
    }
    level = v;
  }
  if (typeof r["at"] !== "string") throw new EngineError("TRUST_POLICY_INVALID", "standing event requires at.");
  parseInstant(r["at"] as string, "at");
  return {
    eventId: (r["event_id"] as string).trim(),
    sourceId: (r["source_id"] as string).trim(),
    transition: transition as StandingEvent["transition"],
    level,
    reason: typeof r["reason"] === "string" ? (r["reason"] as string) : "",
    at: r["at"] as string,
  };
}

/** Pure reducer: standing events -> current standing. Deterministic. */
export function resolveStanding(events: StandingEvent[]): StandingState {
  const ordered = [...events].sort((a, b) => {
    const d = parseInstant(a.at, "at") - parseInstant(b.at, "at");
    if (d !== 0) return d;
    return a.eventId < b.eventId ? -1 : a.eventId > b.eventId ? 1 : 0;
  });
  const revoked = new Set<string>();
  const restricted = new Map<string, TrustLevel>();
  for (const e of ordered) {
    if (e.transition === "revoke") {
      revoked.add(e.sourceId);
      restricted.delete(e.sourceId);
    } else if (e.transition === "restore") {
      revoked.delete(e.sourceId);
      restricted.delete(e.sourceId);
    } else {
      revoked.delete(e.sourceId);
      restricted.set(e.sourceId, e.level ?? "T0");
    }
  }
  return { revoked, restricted };
}

export function decideTrust(
  policy: TrustPolicy,
  standing: StandingState,
  sourceId: string,
  requiredLevel: TrustLevel,
): TrustDecision {
  if (standing.revoked.has(sourceId)) {
    return { sourceId, verdict: "deny", reason: "source is revoked: recall may show it, influence must not use it.", matchedGrantId: null, effectiveLevel: "T0" };
  }
  const cap = standing.restricted.get(sourceId);
  let matchedGrantId: string | null = null;
  let level = policy.defaultLevel;
  for (const grant of policy.grants) {
    if (matchSourcePattern(grant.sourcePattern, sourceId)) {
      matchedGrantId = grant.id;
      level = grant.level;
      break;
    }
  }
  if (cap && TRUST_RANK[cap] < TRUST_RANK[level]) level = cap;
  const verdict: TrustVerdict = TRUST_RANK[level] >= TRUST_RANK[requiredLevel] ? "allow" : "deny";
  return {
    sourceId,
    verdict,
    reason: verdict === "allow"
      ? `effective level ${level} satisfies required ${requiredLevel}.`
      : `effective level ${level} does not satisfy required ${requiredLevel}.`,
    matchedGrantId,
    effectiveLevel: level,
  };
}

const INSTRUCTION_PATTERNS: Array<{ re: RegExp; label: string }> = [
  { re: /ignore\s+(all\s+)?(previous|prior)\s+(rules|instructions|constraints)/i, label: "override-directive" },
  { re: /run\s+(this\s+)?(shell\s+)?command/i, label: "shell-directive" },
  { re: /delete\s+validation/i, label: "validation-deletion" },
  { re: /grant\s+(\S+\s+)?(full|admin)\s+authority/i, label: "authority-grant" },
  { re: /disable\s+(all\s+)?(safety|security|trust)\s+checks/i, label: "safety-disable" },
];

/** Retrieved text is data. Instruction-like content is quarantined, never executed. */
export function screenInstructions(text: string): { clean: boolean; flags: string[] } {
  const flags: string[] = [];
  for (const { re, label } of INSTRUCTION_PATTERNS) {
    if (re.test(text)) flags.push(label);
  }
  return { clean: flags.length === 0, flags };
}
