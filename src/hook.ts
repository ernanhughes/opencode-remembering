import type { ProjectMemoryClient, WorkSignalRequest } from "./client";
import type { RememberingConfig } from "./config";
import {
  extractSessionFacts,
  isMeaningfulQuery,
  latestUserText,
  renderInjectedMemory,
} from "./context";

export type ContextHookEvent = {
  sessionID?: string;
  messages?: unknown[];
  system: Array<{ type: string; text: string }>;
};

/**
 * Build generic WorkSignals from the OpenCode context event.
 *
 * Canonical session capture and WorkSignals serve different purposes:
 * capture asks "what happened" (everything, verbatim); signals ask
 * "what observable evidence describes the work happening now" (latest
 * user request, agent task, newest tool result, test failure). Only a
 * few current observations become signals — never the whole history.
 */
export function extractWorkSignals(
  event: ContextHookEvent,
  facts: { sessionId?: string; agent?: string; messages: Array<{ role: string; text: string; tool_calls?: unknown[]; tool_results?: unknown[] }> },
  nowIso: string,
): WorkSignalRequest[] {
  const signals: WorkSignalRequest[] = [];
  const session = facts.sessionId ?? "adhoc";
  const messages = facts.messages ?? [];

  const latestUser = [...messages].reverse().find(
    (m) => m.role === "user" && m.text.trim(),
  );
  if (latestUser) {
    signals.push({
      signal_id: `signal-user-${session}`,
      kind: "user_message",
      text: latestUser.text,
      observed_at: nowIso,
    });
  }
  if (facts.agent) {
    signals.push({
      signal_id: `signal-agent-${session}`,
      kind: "agent_task",
      text: `agent ${facts.agent}`,
      observed_at: nowIso,
    });
  }
  const toolTexts: string[] = [];
  for (const message of messages) {
    for (const part of [
      ...(message.tool_calls ?? []),
      ...(message.tool_results ?? []),
    ]) {
      const text = JSON.stringify(part).slice(0, 500);
      if (text) toolTexts.push(text);
    }
  }
  if (toolTexts.length > 0) {
    signals.push({
      signal_id: `signal-tool-${session}`,
      kind: "tool_result",
      text: toolTexts[toolTexts.length - 1]!,
      observed_at: nowIso,
    });
  }
  const failure = toolTexts.find((text) =>
    /test.*fail|fail.*test|FAILED|AssertionError|failing/i.test(text),
  );
  if (failure) {
    signals.push({
      signal_id: `signal-test-${session}`,
      kind: "test_failure",
      text: failure.slice(0, 500),
      observed_at: nowIso,
    });
  }
  return signals;
}

/**
 * The automatic OpenCode context behavior, factored out for testing.
 *
 * Order matters and is deliberate:
 *  1. capture canonical session facts (cheap file merge, never blocks);
 *  2. extract the latest useful work signal;
 *  3. determine the route (recall vs influence), establish the frame,
 *     and request a bounded route-aware hybrid bundle;
 *  4. inject it after stable system/provider instructions.
 *
 * A failed memory lookup never destroys an unrelated session: capture
 * errors, empty queries, uninitialized stores and unhealthy backends all
 * degrade to "no injection". Only hard isolation/security invariant
 * violations are rethrown, because those are security invariants.
 */
export function buildContextHook(
  client: ProjectMemoryClient,
  config: RememberingConfig,
) {
  return async (event: ContextHookEvent): Promise<void> => {
    const facts = extractSessionFacts(event as unknown);

    // 1. Canonical capture first: persist what happened before deriving
    //    anything. Best effort; a capture failure must not break context.
    if (facts.sessionId && facts.messages.length > 0) {
      try {
        await client.captureSession(facts.sessionId, {
          agent: facts.agent,
          provider: facts.provider,
          model: facts.model,
          messages: facts.messages,
        });
      } catch (error) {
        console.warn(
          `[opencode-remembering] session capture failed: ${String(error).slice(0, 200)}`,
        );
      }
    }

    if (!config.context.autoInject) return;

    // 2. No meaningful work signal -> no retrieval.
    const query = latestUserText((event.messages ?? []) as unknown[]);
    if (!isMeaningfulQuery(query)) return;

    // 3. Route, frame, trust, then retrieve: the work signal
    //    determines which use the caller is asking memory to serve,
    //    current work signals establish the frame, and standing decides
    //    permission to influence. Automatic injection always uses route
    //    "auto" and work "auto" with the agent as caller scope —
    //    explicit choices are the agent's through memory_context. Every
    //    failure mode below means "memory unavailable", not "session
    //    broken".
    let bundle;
    try {
      bundle = await client.context(
        query,
        config.context.maxChars,
        config.context.maxResults,
        "auto",
        undefined,
        {
          mode: "auto",
          signals: extractWorkSignals(
            event,
            facts as unknown as Parameters<typeof extractWorkSignals>[1],
            new Date().toISOString(),
          ),
        },
        { caller_scope: facts.agent ?? "default" },
      );
    } catch (error) {
      console.warn(
        `[opencode-remembering] memory context failed: ${String(error).slice(0, 200)}`,
      );
      return;
    }
    if (!bundle.indexed || !bundle.content.trim()) return;

    // 4. Inject after the provider's stable prefix (event.system append).
    //    Route, temporal mode, frame decision, trust policy, and
    //    selection policy travel in the wrapper so later policy stages
    //    — and the model — can see which contract this bundle was
    //    served under. Detail stays in the trace.
    const frame = bundle.frame ?? { applied: false };
    event.system.push({
      type: "text",
      text: renderInjectedMemory(
        bundle.trace_id,
        bundle.schema,
        bundle.content,
        bundle.route.route,
        bundle.temporal.mode,
        frame.applied
          ? {
              workType: frame.establishment?.work_type ?? undefined,
              establishment: frame.establishment?.establishment,
              control: frame.control,
            }
          : {},
        bundle.trust?.policy_version ?? undefined,
        bundle.selection?.policy_version ?? undefined,
      ),
    });
  };
}

/** Fail-closed gate used at plugin startup. */
export function isSecurityFailure(error: unknown): boolean {
  const message = String(error);
  return (
    message.includes("SCHEMA_MISMATCH") ||
    message.includes("FRAME_PROJECT_MISMATCH") ||
    message.includes("CONFIG_INVALID") ||
    message.includes("refusing")
  );
}
