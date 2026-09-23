import { readFile } from "node:fs/promises";
import * as path from "node:path";

import { EngineError } from "../errors";
import { TEMPORAL_EVENT_SCHEMA, TEMPORAL_STORE_VERSION, validateEnvelope, validateStandpoint, type TemporalStandpoint } from "./model";
import { reduceTemporalLog, resolveAtStandpoint } from "./reducer";
import { MemoryTemporalStore, type TemporalEventStore } from "./store";

export type TemporalImportSummary = {
  imported: number;
  duplicates: number;
  failed: string[];
  events: number;
  subjects: string[];
};

export class TemporalService {
  constructor(private readonly store: TemporalEventStore = new MemoryTemporalStore()) {}

  async importLines(lines: string[], receivedAtFallback: string): Promise<TemporalImportSummary> {
    let imported = 0;
    let duplicates = 0;
    const failed: string[] = [];
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i] as string;
      if (!line.trim()) continue;
      let raw: unknown;
      try {
        raw = JSON.parse(line);
      } catch (error) {
        failed.push(`line ${i + 1}: not JSON.`);
        continue;
      }
      let envelope;
      try {
        envelope = validateEnvelope(raw, receivedAtFallback);
      } catch (error) {
        const code = error instanceof EngineError ? error.code : "TEMPORAL_EVENT_INVALID";
        failed.push(`line ${i + 1}: ${code}: malformed envelope.`);
        continue;
      }
      try {
        const out = await this.store.append(envelope);
        if (out.duplicate) duplicates += 1;
        else imported += 1;
      } catch (error) {
        const code = error instanceof EngineError ? error.code : "TEMPORAL_CONFLICT";
        failed.push(`line ${i + 1}: ${code}:${envelope.eventId}`);
      }
    }
    return {
      imported, duplicates, failed,
      events: await this.store.count(),
      subjects: await this.store.subjects(),
    };
  }

  async importFile(projectDirectory: string): Promise<TemporalImportSummary> {
    const file = path.join(projectDirectory, ".remembering", "temporal", "events.jsonl");
    let content: string;
    try {
      content = await readFile(file, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        return { imported: 0, duplicates: 0, failed: [], events: await this.store.count(), subjects: await this.store.subjects() };
      }
      throw error;
    }
    return this.importLines(content.split("\n"), new Date().toISOString());
  }

  async state(subject: string, standpointRaw: { mode?: string; valid_at?: string; known_at?: string } = {}) {
    if (!subject.trim()) throw new EngineError("TEMPORAL_EVENT_INVALID", "subject is required.");
    const standpoint: TemporalStandpoint = validateStandpoint(standpointRaw);
    const events = await this.store.list();
    const resolved = resolveAtStandpoint(events, subject, standpoint);
    const report = reduceTemporalLog(events);
    return {
      ...resolved,
      standpoint: {
        mode: standpoint.mode,
        valid_at: standpoint.validAt ?? null,
        known_at: standpoint.knownAt ?? null,
      },
      trajectory_truncated: false,
      storeVersion: TEMPORAL_STORE_VERSION,
      eventSchemaVersion: TEMPORAL_EVENT_SCHEMA,
      reducerVersion: report.reducerVersion,
    };
  }

  async health() {
    const events = await this.store.list();
    let report;
    try {
      report = reduceTemporalLog(events);
    } catch (error) {
      return {
        storeReady: true, events: events.length, subjects: [],
        unknownReferences: [], causalityViolations: [], sequenceGaps: [],
        error: error instanceof Error ? error.message.slice(0, 160) : String(error).slice(0, 160),
      };
    }
    const versions = await this.store.versions();
    return {
      storeReady: true,
      storeVersion: versions.storeVersion,
      eventSchemaVersion: versions.eventSchemaVersion,
      events: events.length,
      subjects: [...report.subjects.keys()],
      unknownReferences: report.unknownReferences,
      causalityViolations: report.causalityViolations,
      sequenceGaps: report.sequenceGaps,
    };
  }
}
