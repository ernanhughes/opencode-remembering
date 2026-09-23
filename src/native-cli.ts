#!/usr/bin/env bun
/**
 * Native-engine CLI (Stage 3). Same config surface as dev-cli.ts
 * but executes the TypeScript engine directly — no subprocess, no legacy runtime:
 *
 *   bun src/native-cli.ts <command> [query] [--dir <path>] [--limit N]
 *     [--route auto|recall|influence] [--subject S] [--mode MODE]
 *     [--valid-at ISO] [--known-at ISO] [--loop-id ID] [--record ID]
 *     [--content TEXT] [--target ID] [--role R] [--reason R] [--trace ID]
 *     [--candidate ID] [--with ID] [--kind trust|selection]
 *
 * Commands: doctor setup refresh search context state temporal-import
 *   frame-health trust-health trace loop-health loop-list loop-show
 *   loop-history remember write-health write-history write-rebuild
 *   record-show
 */

import { loadConfig } from "./config";
import { RememberingEngine } from "./engine/pipeline";

function usage(): never {
  console.error(
    "usage: native-cli.ts <doctor|setup|refresh|search|context|state|temporal-import|frame-health|trust-health|trace|loop-health|loop-list|loop-show|loop-history|remember|write-health|write-history|write-rebuild|record-show> [args] [--dir <path>] ...",
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
const limitFlag = flagValue("--limit");
const limit = limitFlag !== undefined ? Number(limitFlag) : 8;
const routeFlag = flagValue("--route") as "auto" | "recall" | "influence" | undefined;
const subjectFlag = flagValue("--subject");
const modeFlag = flagValue("--mode");
const validAtFlag = flagValue("--valid-at");
const knownAtFlag = flagValue("--known-at");
const loopIdFlag = flagValue("--loop-id");
const recordFlag = flagValue("--record");
const contentFlag = flagValue("--content");
const targetFlag = flagValue("--target");
const roleFlag = flagValue("--role");
const reasonFlag = flagValue("--reason");
const traceFlag = flagValue("--trace");
const candidateFlag = flagValue("--candidate");
const withFlag = flagValue("--with");
const kindFlag = flagValue("--kind");
const traceSub = rawArgs[1] ?? "get";
const rest = rawArgs.slice(1);

const config = await loadConfig(directory);
const engine = new RememberingEngine({
  dsn: config.dsn,
  schema: config.schema,
  projectDirectory: directory,
  embedding: {
    provider: config.embedding.provider as "ollama" | "sentence-transformers" | "hashing",
    model: config.embedding.model,
    host: config.embedding.host,
  },
  retrieval: {
    mode: config.retrieval.mode,
    lexicalK: config.retrieval.lexicalK,
    denseK: config.retrieval.denseK,
    fusionK: config.retrieval.fusionK,
    rerankK: config.retrieval.rerankK,
    reranker: config.retrieval.reranker,
  },
});

let result: unknown;
try {
  switch (command) {
    case "doctor":
      result = await engine.nativeDoctor();
      break;
    case "setup":
      result = await engine.setup();
      break;
    case "refresh":
      result = await engine.refresh();
      break;
    case "search":
      result = await engine.search(rest.join(" ") || "test", limit);
      break;
    case "context":
      result = await engine.context(
        rest.join(" ") || "test",
        config.context.maxChars,
        config.context.maxResults,
        routeFlag ?? "auto",
        modeFlag || validAtFlag || knownAtFlag
          ? { mode: modeFlag, valid_at: validAtFlag, known_at: knownAtFlag }
          : undefined,
      );
      break;
    case "state":
      result = await engine.state(subjectFlag ?? rest.join(" ") ?? "", {
        query: rest.join(" "),
        route: routeFlag ?? "auto",
        temporal: modeFlag || validAtFlag || knownAtFlag
          ? { mode: modeFlag, valid_at: validAtFlag, known_at: knownAtFlag }
          : undefined,
      });
      break;
    case "temporal-import":
      result = await engine.temporalImport();
      break;
    case "frame-health":
      result = { ...(await engine.nativeDoctor())["frame"] as Record<string, unknown> };
      break;
    case "trust-health":
      result = { ...(await engine.nativeDoctor())["trust"] as Record<string, unknown> };
      break;
    case "trace":
      if (traceSub === "get") result = await engine.traceGet(traceFlag ?? rawArgs[2] ?? "");
      else if (traceSub === "explain") result = await engine.traceExplain(traceFlag ?? rawArgs[2] ?? "", candidateFlag ?? rawArgs[3] ?? "");
      else if (traceSub === "verify") result = await engine.traceVerify(traceFlag ?? rawArgs[2] ?? "");
      else if (traceSub === "replay") result = await engine.traceReplay(traceFlag ?? rawArgs[2] ?? "", (kindFlag as "trust" | "selection" | undefined) ?? "trust", rawArgs.includes("--persist"));
      else if (traceSub === "diff") result = await engine.traceDiff(traceFlag ?? rawArgs[2] ?? "", withFlag ?? rawArgs[3] ?? "");
      else result = await engine.traceFind({});
      break;
    case "loop-health":
      result = { ...(await engine.nativeDoctor())["loops"] as Record<string, unknown> };
      break;
    case "loop-list":
      result = await engine.openLoops({ action: "list", state: "open" });
      break;
    case "loop-show":
      result = await engine.openLoops({ action: "get", loop_id: loopIdFlag ?? rawArgs[1] ?? "" });
      break;
    case "loop-history":
      result = await engine.openLoops({ action: "history", loop_id: loopIdFlag ?? rawArgs[1] ?? "" });
      break;
    case "remember":
      result = await engine.remember({
        action: "remember",
        content: contentFlag ?? rest.join(" ") ?? "",
        role: (roleFlag as "ordinary" | undefined) ?? "ordinary",
        reason: reasonFlag,
        callerScope: "cli",
        origin: "cli",
      });
      break;
    case "write-health":
      result = { ...(await engine.nativeDoctor())["writes"] as Record<string, unknown> };
      break;
    case "write-history":
      result = await engine.writeHistory(recordFlag ?? rawArgs[1] ?? "");
      break;
    case "write-rebuild":
      result = await engine.writeRebuild();
      break;
    case "record-show":
      result = await engine.recordShow(recordFlag ?? rawArgs[1] ?? "");
      break;
    default:
      usage();
  }
} catch (error) {
  const code = (error as { code?: string })?.code ?? "ENGINE_ERROR";
  const message = error instanceof Error ? error.message : String(error);
  result = { ok: false, native: true, code, message: message.slice(0, 500) };
} finally {
  await engine.close();
}

console.log(JSON.stringify(result, null, 2));
if (typeof result === "object" && result !== null && "ok" in result && (result as { ok: unknown }).ok === false) {
  process.exitCode = 1;
}
