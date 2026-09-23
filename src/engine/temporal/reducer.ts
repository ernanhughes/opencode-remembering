import { EngineError } from "../errors";
import {
  parseInstant,
  TEMPORAL_REDUCER_VERSION,
  type TemporalEnvelope,
  type TemporalStandpoint,
} from "./model";

export type SubjectStatus =
  | "current"
  | "historical"
  | "planned"
  | "retracted"
  | "superseded"
  | "unknown";

export type TrajectoryEntry = {
  eventId: string;
  kind: string;
  value: string | null;
  eventTime: string;
  knownTime: string;
  effectiveFrom: string;
  supersededBy: string | null;
};

export type ResolvedSubject = {
  subject: string;
  value: string | null;
  status: SubjectStatus;
  provenance: string[];
  trajectory: TrajectoryEntry[];
  incompleteHistory: boolean;
  reason: string;
};

export type ReduceReport = {
  subjects: Map<string, ResolvedSubject>;
  unknownReferences: string[];
  causalityViolations: string[];
  sequenceGaps: Array<{ subject: string; after: string; before: string }>;
  reducerVersion: string;
};

function knownOf(e: TemporalEnvelope): string {
  return e.knownTime ?? e.receivedAt;
}

function effectiveOf(e: TemporalEnvelope): string {
  return e.effectiveFrom ?? e.eventTime;
}

/**
 * Pure append-only reducer: events -> resolved per-subject state.
 * Never mutates its input. Deterministic for the same event set.
 */
export function reduceTemporalLog(envelopes: TemporalEnvelope[]): ReduceReport {
  const sorted = [...envelopes].sort((a, b) => {
    const ka = parseInstant(knownOf(a), "known_time");
    const kb = parseInstant(knownOf(b), "known_time");
    if (ka !== kb) return ka - kb;
    return a.eventId < b.eventId ? -1 : a.eventId > b.eventId ? 1 : 0;
  });
  const byId = new Map<string, TemporalEnvelope>();
  const conflicts: string[] = [];
  for (const e of sorted) {
    const prior = byId.get(e.eventId);
    if (prior) {
      const same =
        prior.subject === e.subject && prior.kind === e.kind &&
        prior.value === e.value && prior.eventTime === e.eventTime;
      if (!same) conflicts.push(e.eventId);
      continue;
    }
    byId.set(e.eventId, e);
  }
  if (conflicts.length > 0) {
    throw new EngineError("TEMPORAL_CONFLICT", `conflicting reuse of event identity: ${conflicts.join(", ")}.`);
  }

  const unknownReferences: string[] = [];
  const causalityViolations: string[] = [];
  const sequenceGaps: Array<{ subject: string; after: string; before: string }> = [];
  const bySubject = new Map<string, TemporalEnvelope[]>();
  for (const e of sorted) {
    if (e.supersedes && !byId.has(e.supersedes)) unknownReferences.push(e.supersedes);
    if (parseInstant(knownOf(e), "known_time") < parseInstant(e.eventTime, "event_time")) {
      causalityViolations.push(e.eventId);
    }
    const list = bySubject.get(e.subject) ?? [];
    list.push(e);
    bySubject.set(e.subject, list);
  }

  const subjects = new Map<string, ResolvedSubject>();
  for (const [subject, list] of bySubject) {
    subjects.set(subject, resolveSubject(subject, list));
  }
  return {
    subjects,
    unknownReferences: [...new Set(unknownReferences)].sort(),
    causalityViolations: [...new Set(causalityViolations)].sort(),
    sequenceGaps,
    reducerVersion: TEMPORAL_REDUCER_VERSION,
  };
}

function resolveSubject(subject: string, list: TemporalEnvelope[]): ResolvedSubject {
  const ordered = [...list].sort((a, b) => {
    const ea = parseInstant(effectiveOf(a), "effective_from");
    const eb = parseInstant(effectiveOf(b), "effective_from");
    if (ea !== eb) return ea - eb;
    return parseInstant(knownOf(a), "known_time") - parseInstant(knownOf(b), "known_time");
  });
  const trajectory: TrajectoryEntry[] = ordered.map((e) => ({
    eventId: e.eventId,
    kind: e.kind,
    value: e.value,
    eventTime: e.eventTime,
    knownTime: knownOf(e),
    effectiveFrom: effectiveOf(e),
    supersededBy: null,
  }));
  const byId = new Map(trajectory.map((t) => [t.eventId, t]));
  for (const e of ordered) {
    if ((e.kind === "supersede" || e.kind === "correct") && e.supersedes) {
      const target = byId.get(e.supersedes);
      if (target) target.supersededBy = e.eventId;
    }
  }
  const last = ordered[ordered.length - 1];
  if (!last) {
    return { subject, value: null, status: "unknown", provenance: [], trajectory, incompleteHistory: true, reason: "no events for subject." };
  }
  const maxKnown = Math.max(...ordered.map((e) => parseInstant(knownOf(e), "known_time")));
  const lastEffective = parseInstant(effectiveOf(last), "effective_from");
  if (lastEffective > maxKnown) {
    return {
      subject, value: last.value, status: "planned", provenance: [last.eventId],
      trajectory, incompleteHistory: false,
      reason: `latest event ${last.eventId} is future-effective; current value withheld.`,
    };
  }
  if (last.kind === "retract") {
    return {
      subject, value: null, status: "retracted", provenance: [last.eventId],
      trajectory, incompleteHistory: false,
      reason: `retracted by ${last.eventId}; history preserved.`,
    };
  }
  const superseded = trajectory.some((t) => t.supersededBy !== null);
  return {
    subject,
    value: last.value,
    status: superseded && ordered.length > 1 && last.kind !== "assert" ? "superseded" : "current",
    provenance: [last.eventId],
    trajectory,
    incompleteHistory: false,
    reason: `resolved to ${last.eventId} (${last.kind}).`,
  };
}

/** Resolve one subject under an explicit standpoint. */
export function resolveAtStandpoint(
  envelopes: TemporalEnvelope[],
  subject: string,
  standpoint: TemporalStandpoint,
): ResolvedSubject {
  let visible = envelopes.filter((e) => e.subject === subject);
  if (visible.length === 0) {
    return {
      subject, value: null, status: "unknown", provenance: [], trajectory: [],
      incompleteHistory: true, reason: "no events for subject.",
    };
  }
  if (standpoint.mode === "as_known" || standpoint.mode === "bitemporal") {
    const knownAt = parseInstant(standpoint.knownAt as string, "known_at");
    visible = visible.filter((e) => parseInstant(knownOf(e), "known_time") <= knownAt);
    if (visible.length === 0) {
      return {
        subject, value: null, status: "unknown", provenance: [], trajectory: [],
        incompleteHistory: true, reason: `no events known at ${standpoint.knownAt}.`,
      };
    }
  }
  if (standpoint.mode === "valid_at" || standpoint.mode === "bitemporal") {
    const validAt = parseInstant(standpoint.validAt as string, "valid_at");
    const eligible = visible.filter((e) => parseInstant(effectiveOf(e), "effective_from") <= validAt);
    if (eligible.length === 0) {
      const earliest = [...visible].sort((a, b) =>
        parseInstant(effectiveOf(a), "effective_from") - parseInstant(effectiveOf(b), "effective_from"))[0];
      return {
        subject, value: null, status: "planned", provenance: [],
        trajectory: toTrajectory(visible), incompleteHistory: false,
        reason: `nothing effective at ${standpoint.validAt}; earliest is ${(earliest as TemporalEnvelope).eventId}.`,
      };
    }
    const winner = [...eligible].sort((a, b) => {
      const d = parseInstant(effectiveOf(b), "effective_from") - parseInstant(effectiveOf(a), "effective_from");
      if (d !== 0) return d;
      return parseInstant(knownOf(b), "known_time") - parseInstant(knownOf(a), "known_time");
    })[0] as TemporalEnvelope;
    const status: SubjectStatus = winner.kind === "retract" ? "retracted" : "historical";
    return {
      subject, value: winner.value, status, provenance: [winner.eventId],
      trajectory: toTrajectory(visible), incompleteHistory: false,
      reason: `resolved at standpoint ${standpoint.mode} to ${winner.eventId}.`,
    };
  }
  const resolved = resolveSubject(subject, visible);
  return resolved;
}

function toTrajectory(list: TemporalEnvelope[]): TrajectoryEntry[] {
  return [...list]
    .sort((a, b) => parseInstant(knownOf(a), "known_time") - parseInstant(knownOf(b), "known_time"))
    .map((e) => ({
      eventId: e.eventId,
      kind: e.kind,
      value: e.value,
      eventTime: e.eventTime,
      knownTime: knownOf(e),
      effectiveFrom: effectiveOf(e),
      supersededBy: null,
    }));
}
