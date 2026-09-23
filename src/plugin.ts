import { Plugin } from "@opencode/plugin";
import type { Info as ToolInfo } from "@opencode/plugin/promise/tool";

import { ProjectMemoryClient } from "./client";
import { loadConfig } from "./config";
import { buildContextHook, isSecurityFailure } from "./hook";
import {
  MemoryContext,
  MemoryHealth,
  MemoryOpenLoops,
  MemoryRefresh,
  MemorySearch,
  MemorySetup,
  MemoryState,
  MemoryTemporalImport,
  MemoryTrace,
} from "./tools";

const RememberingPlugin = Plugin.define({
  id: "opencode-remembering",

  async setup(ctx) {
    const directory = ctx.location.directory;
    // Malformed isolation/security configuration fails closed here.
    const config = await loadConfig(directory);
    const client = new ProjectMemoryClient(config, directory);

    // Readiness probe, not a hard gate: an unavailable memory backend must
    // not destroy an unrelated OpenCode session. Only hard
    // isolation/security invariant violations abort plugin startup.
    try {
      const health = await client.doctor();
      if (!health.ok) {
        console.warn(
          `[opencode-remembering] memory not ready: ${health.message ?? "see memory_health"}`,
        );
      }
      if (!health.schema_identity_ok) {
        throw new Error(
          `SCHEMA_MISMATCH: project schema identity check failed: ${health.message ?? ""}`,
        );
      }
    } catch (error) {
      if (isSecurityFailure(error)) throw error;
      console.warn(
        `[opencode-remembering] memory unavailable at startup: ${String(error).slice(0, 300)}`,
      );
    }

    const tools: ToolInfo[] = [
      MemoryHealth(client),
      MemorySetup(client),
      MemoryRefresh(client),
      MemorySearch(client),
      MemoryContext(client),
      MemoryState(client),
      MemoryTemporalImport(client),
      MemoryTrace(client),
      MemoryOpenLoops(client),
    ];

    await ctx.tool.transform((editor) => {
      for (const tool of tools) editor.add(tool);
    });

    const onContext = buildContextHook(client, config);
    await ctx.session.hook("context", async (event) => {
      await onContext(
        event as unknown as Parameters<typeof onContext>[0],
      );
    });
  },
});

export default RememberingPlugin;
