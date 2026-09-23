#!/usr/bin/env bun
/**
 * Development CLI around the Python bridge. Same adapter path as the
 * OpenCode plugin (config -> client -> bridge -> bundled engine), usable
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
    "usage: dev-cli.ts <doctor|setup|refresh|search|context|route-eval|temporal-import|temporal-state|temporal-eval|frame-health|frame-eval|trust-health|trust-import|trust-eval> [query] [--dir <path>] [--route auto|recall|influence] [--temporal-mode MODE] [--valid-at ISO] [--known-at ISO] [--subject S] [--work-mode auto|explicit|none] [--work-type T] [--objective O] [--caller-scope S] [--trust-level FULL]",
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
const rest = rawArgs.slice(1);

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
