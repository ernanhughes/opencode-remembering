#!/usr/bin/env bun
/**
 * Development CLI around the native engine. Same adapter path as the
 * OpenCode plugin (config -> client -> native engine), usable
 * without running OpenCode:
 *
 *   bun src/dev-cli.ts doctor
 *   bun src/dev-cli.ts setup
 *   bun src/dev-cli.ts refresh
 *   bun src/dev-cli.ts search "hybrid retrieval"
 *   bun src/dev-cli.ts context "how does setup work"
 *
 * The project directory defaults to the current working directory.
 */

import { ProjectMemoryClient } from "./client";
import type { TemporalStandpointRequest } from "./client";
import { loadConfig } from "./config";

function usage(): never {
  console.error(
    "usage: dev-cli.ts <doctor|setup|refresh|search|context|route-eval|temporal-import|temporal-state|temporal-eval|frame-health|frame-eval|trust-health|trust-import|trust-eval|trace|trace-eval|loop-health|loop-import|loop-list|loop-show|loop-history|loop-eval|loop-rebuild|loop-create|remember|correct|supersede|retract|write-health|write-eval|write-rebuild|record-show|action-show|write-history> [query] [--dir <path>] [--route auto|recall|influence] [--temporal-mode MODE] [--valid-at ISO] [--known-at ISO] [--subject S] [--work-mode auto|explicit|none] [--work-type T] [--objective O] [--caller-scope S] [--trust-level FULL] [--selection decisive]",
  );
  process.exit(2);
}

const rawArgs = process.argv.slice(2);
const command = rawArgs[0];
if (!command) usage();

let directory = process.cwd();
const dirFlag = rawArgs.indexOf("--dir");
if (dirFlag !== -1) {
  directory = rawArgs[dirFlag + 1] ?? directory;
  rawArgs.splice(dirFlag, 2);
}
function flagValue(name: string): string | undefined {
  const flag = rawArgs.indexOf(name);
  if (flag === -1) return undefined;
  const value = rawArgs[flag + 1];
  rawArgs.splice(flag, 2);
  return value;
}

function routeArg(): "auto" | "recall" | "influence" {
  const value = flagValue("--route");
  if (value === undefined) return "auto";
  if (value === "recall" || value === "influence" || value === "auto") {
    return value;
  }
  console.error(`unknown --route ${JSON.stringify(value)}`);
  process.exit(2);
}

function temporalArg():
  | { mode?: string; valid_at?: string; known_at?: string }
  | undefined {
  const mode = flagValue("--temporal-mode");
  const valid_at = flagValue("--valid-at");
  const known_at = flagValue("--known-at");
  if (mode === undefined && valid_at === undefined && known_at === undefined) {
    return undefined;
  }
  return { mode, valid_at, known_at };
}
const requestedRoute = routeArg();
const requestedTemporal = temporalArg() as
  | TemporalStandpointRequest
  | undefined;
const subjectFlag = flagValue("--subject");
const workMode = flagValue("--work-mode");
const workType = flagValue("--work-type");
const objectiveFlag = flagValue("--objective");
const callerScope = flagValue("--caller-scope");
const trustLevel = flagValue("--trust-level");
const selectionMode = flagValue("--selection");
const requestedWork =
  workMode === undefined &&
  workType === undefined &&
  objectiveFlag === undefined
    ? undefined
    : {
        mode: (workMode ?? "auto") as "auto" | "explicit" | "none",
        work_type: workType,
        objective: objectiveFlag,
      };
function loopCreateArgs(): Record<string, unknown> {
  const getFlag = (name: string): string | undefined => {
    const flag = rawArgs.indexOf(name);
    if (flag === -1) return undefined;
    const value = rawArgs[flag + 1];
    rawArgs.splice(flag, 2);
    return value;
  };
  const loopId = rawArgs[1] ?? getFlag("--loop-id") ?? "";
  const subject = getFlag("--subject") ?? "";
  const kind =
    (getFlag("--kind") as string | undefined) ?? "TASK";
  const expected = getFlag("--expected") ?? "";
  const fromState = getFlag("--from") ?? "";
  const closureJson = getFlag("--closure") ?? "[]";
  let closure: unknown = [];
  try {
    closure = JSON.parse(closureJson);
  } catch {
    console.error("loop-create --closure must be JSON");
    process.exit(2);
  }
  return {
    loop_id: loopId,
    subject,
    transition_kind: kind,
    from_state: fromState,
    expected_state: expected,
    evidence_refs: [],
    closure_kind: "all",
    closure,
  };
}

const rest = rawArgs.slice(1);

function traceArgs(): import("./client").TraceRequest {
  const sub = (rawArgs[1] ?? "get").toLowerCase();
  const getFlag = (name: string): string | undefined => {
    const flag = rawArgs.indexOf(name);
    if (flag === -1) return undefined;
    const value = rawArgs[flag + 1];
    rawArgs.splice(flag, 2);
    return value;
  };
  if (sub === "get" || sub === "verify") {
    const traceId = rawArgs[2] ?? getFlag("--trace-id") ?? "";
    return { mode: sub, trace_id: traceId } as import("./client").TraceRequest;
  }
  if (sub === "explain") {
    return {
      mode: "explain",
      trace_id: rawArgs[2] ?? "",
      candidate_id: rawArgs[3] ?? getFlag("--candidate") ?? "",
    };
  }
  if (sub === "replay") {
    return {
      mode: "replay",
      trace_id: rawArgs[2] ?? "",
      replay_kind:
        (getFlag("--kind") as "trust" | "selection" | undefined) ?? "trust",
      selection_mode:
        (getFlag("--selection-mode") as "decisive" | "full" | undefined) ??
        "full",
      persist: rawArgs.includes("--persist"),
    };
  }
  if (sub === "diff") {
    return {
      mode: "diff",
      trace_id: rawArgs[2] ?? "",
      diff_with: rawArgs[3] ?? getFlag("--with") ?? "",
    };
  }
  // find
  const filters: Record<string, unknown> = {};
  for (const key of [
    "source",
    "chunk",
    "route",
    "work-type",
    "policy-stage",
    "policy-version",
    "terminal-stage",
  ]) {
    const value = getFlag(`--${key}`);
    if (value !== undefined) {
      filters[
        key === "source"
          ? "source_id"
          : key === "chunk"
            ? "chunk_id"
            : key.replace(/-/g, "_")
      ] = value;
    }
  }
  const limit = getFlag("--limit");
  if (limit !== undefined) filters.limit = Number(limit);
  return { mode: "find", filters } as import("./client").TraceRequest;
}

const config = await loadConfig(directory);
const client = new ProjectMemoryClient(config, directory);

let result: unknown;
switch (command) {
  case "doctor":
    result = await client.doctor();
    break;
  case "setup":
    result = await client.setup();
    break;
  case "refresh":
    result = await client.refresh();
    break;
  case "search":
    result = await client.search(rest.join(" ") || "test", 8);
    break;
  case "context":
    result = await client.context(
      rest.join(" ") || "test",
      config.context.maxChars,
      config.context.maxResults,
      requestedRoute,
      requestedTemporal,
      requestedWork,
      callerScope === undefined && trustLevel === undefined
        ? undefined
        : {
            caller_scope: callerScope,
            level: trustLevel as
              | "T0"
              | "S1"
              | "S2"
              | "S3"
              | "FULL"
              | undefined,
          },
      selectionMode === undefined
        ? undefined
        : {
            mode: selectionMode as "decisive" | "full",
          },
    );
    break;
  case "route-eval":
    result = await client.routeEval();
    break;
  case "temporal-import":
    result = await client.temporalImport();
    break;
  case "temporal-state":
    result = await client.state(
      subjectFlag ?? rest.join(" ") ?? "",
      {
        query: rest.join(" "),
        route: requestedRoute,
        temporal: requestedTemporal,
      },
    );
    break;
  case "temporal-eval":
    result = await client.temporalEval();
    break;
  case "frame-health":
    result = (await client.doctor()).frame;
    break;
  case "frame-eval":
    result = await client.frameEval();
    break;
  case "trust-health":
    result = (await client.doctor()).trust;
    break;
  case "trust-import":
    result = await client.trustImport();
    break;
  case "trust-eval":
    result = await client.trustEval();
    break;
  case "selection-eval":
    result = await client.selectionEval();
    break;
  case "trace":
    result = await client.trace(traceArgs());
    break;
  case "trace-eval":
    result = await client.traceEval();
    break;
  case "loop-health":
    result = (await client.doctor()).loops;
    break;
  case "loop-import":
    result = await client.loopImport();
    break;
  case "loop-list":
    result = await client.openLoops({
      action: "list",
      state: (flagValue("--state") as "open" | undefined) ?? "open",
      subject: flagValue("--subject"),
      limit: flagValue("--limit") ? Number(flagValue("--limit")) : undefined,
    });
    break;
  case "loop-show":
    result = await client.openLoops({
      action: "get",
      loop_id: rawArgs[1] ?? flagValue("--loop-id") ?? "",
    });
    break;
  case "loop-history":
    result = await client.openLoops({
      action: "history",
      loop_id: rawArgs[1] ?? flagValue("--loop-id") ?? "",
    });
    break;
  case "loop-eval":
    result = await client.loopEval();
    break;
  case "loop-rebuild":
    result = await client.loopRebuild();
    break;
  case "loop-create":
    result = await client.loopCreate(loopCreateArgs());
    break;
  case "remember":
  case "correct":
  case "supersede":
  case "retract": {
    const action = command as "remember" | "correct" | "supersede" | "retract";
    const contentFlag = flagValue("--content");
    const content = contentFlag ?? rest.join(" ") ?? "";
    const evidenceFlag = flagValue("--evidence");
    result = await client.remember({
      action,
      content,
      target_record_id:
        flagValue("--target") ?? (action === "remember" ? undefined : rawArgs[1]),
      role: (flagValue("--role") as
        | "ordinary"
        | "evidence"
        | "proposal"
        | "preference"
        | "decision"
        | "production_state"
        | undefined) ?? "ordinary",
      reason: flagValue("--reason"),
      evidence_refs: evidenceFlag
        ? evidenceFlag.split(",").map((s) => s.trim()).filter(Boolean)
        : undefined,
      effective_from: flagValue("--effective-from"),
      event_time: flagValue("--event-time"),
      idempotency_key: flagValue("--idempotency-key"),
      caller_scope: flagValue("--caller-scope") ?? callerScope,
      origin: "cli",
    });
    break;
  }
  case "write-health":
    result = (await client.doctor()).writes;
    break;
  case "write-eval":
    result = await client.writeEval();
    break;
  case "write-rebuild":
    result = await client.writeRebuild();
    break;
  case "record-show":
    result = await client.recordShow(
      rawArgs[1] ?? flagValue("--record") ?? "",
    );
    break;
  case "action-show":
    result = await client.actionShow(
      rawArgs[1] ?? flagValue("--action") ?? "",
    );
    break;
  case "write-history":
    result = await client.writeHistory(
      flagValue("--record") ?? rawArgs[1] ?? "",
    );
    break;
  default:
    usage();
}

console.log(JSON.stringify(result, null, 2));
if (
  typeof result === "object" &&
  result !== null &&
  "ok" in result &&
  (result as { ok: unknown }).ok === false
) {
  process.exitCode = 1;
}
