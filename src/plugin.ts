import { Plugin } from "@opencode/plugin";
import type { Info as ToolInfo } from "@opencode/plugin/promise/tool";

import { ProjectMemoryClient } from "./client";
import { loadConfig } from "./config";
import { latestUserText, renderInjectedMemory } from "./context";
import { MemoryContext, MemoryHealth, MemorySearch } from "./tools";

const RememberingPlugin = Plugin.define({
  id: "opencode-remembering",

  async setup(ctx) {
    const directory = ctx.location.directory;
    const config = await loadConfig(directory);
    const client = new ProjectMemoryClient(config, directory);

    // Hard prerequisite gate. We deliberately do not fall back to an
    // embedded store, SQLite, or a global file journal.
    const health = await client.doctor();
    if (!health.ok) {
      throw new Error("Project Memory prerequisites are not healthy.");
    }

    const tools: ToolInfo[] = [
      MemoryHealth(client),
      MemorySearch(client),
      MemoryContext(client),
    ];

    await ctx.tool.transform((editor) => {
      for (const tool of tools) editor.add(tool);
    });

    if (config.context.autoInject) {
      await ctx.session.hook("context", async (event) => {
        const query = latestUserText(event.messages as unknown[]);
        if (!query) return;

        const bundle = await client.context(
          query,
          config.context.maxChars,
          config.context.maxResults,
        );

        if (!bundle.indexed || !bundle.content.trim()) return;

        event.system.push({
          type: "text",
          text: renderInjectedMemory(bundle.trace_id, bundle.schema, bundle.content),
        });
      });
    }
  },
});

export default RememberingPlugin;
