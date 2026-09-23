# OpenCode handoff

This repository has been bootstrapped far enough for OpenCode to take over implementation.

The architectural decision is now fixed:

> `opencode-remembering` is the OpenCode adapter. `ernanhughes/project-memory` is the memory implementation. PostgreSQL + pgvector are required.

Do not create a second memory engine in TypeScript.

## Current bootstrap

The repository currently provides:

1. OpenCode v2 plugin registration.
2. Hard prerequisite checks.
3. Per-project PostgreSQL schema isolation.
4. A Python bridge that imports `project-memory/solution/memory_baseline/storage.py`.
5. `memory_health`.
6. `memory_search` using Project Memory's PostgreSQL full-text search.
7. `memory_context` plus optional context-hook injection.
8. Tests for message extraction/rendering.

This is intentionally the thinnest real vertical slice.

## Stage 1 — make indexing real

Implement automatic ingestion before adding more memory mechanisms.

Goal:

```text
OpenCode project
→ project-memory ingestion
→ PostgreSQL sources/chunks
→ tsvector + pgvector
→ hybrid search
→ OpenCode context
```

### 1A. Expose a stable Project Memory application API

Prefer implementing this in `ernanhughes/project-memory` rather than growing this bridge.

Create a small production-facing API/service layer around the already-tested modules.

It should expose operations approximately equivalent to:

- `health(project)`
- `refresh(project_root, project_id)`
- `search(project_id, query, options)`
- `context(project_id, work_signal, options)`

The API must return structured traces, not prose-only responses.

Do not move the underlying implementations out of Project Memory merely to make them convenient to call.

### 1B. Repository ingestion

Use the existing `solution/memory_baseline` ingestion, chunking, storage, embedding, retrieval and context code where possible.

Require PostgreSQL + pgvector.

Do not add SQLite.

Do not silently fall back to hashing embeddings for production. Project Memory explicitly labels the hashing embedder as a deterministic test double.

Make the production embedding provider configurable. The current Project Memory default is Ollama `bge-m3`; retain that as a supported path, but keep the adapter boundary clean.

### 1C. OpenCode session ingestion

Capture canonical OpenCode history automatically.

Persist unseen session/message/tool events idempotently.

Store canonical event/source identity before deriving summaries.

Do not require the agent to call a "remember" tool for ordinary history to exist.

Preserve:

- session ID,
- message ID,
- role,
- agent,
- model/provider when available,
- tool call identity,
- tool result identity,
- observed timestamp,
- project identity.

Treat model-produced summaries as derived records, not canonical history.

### 1D. Hybrid retrieval

Replace the bootstrap lexical-only bridge call with Project Memory's real retrieval pipeline:

- PostgreSQL full-text search,
- pgvector dense search,
- reciprocal-rank fusion,
- optional reranking,
- bounded result counts,
- retrieval trace.

Every returned candidate must retain source/chunk identity.

Add tests proving the pgvector path actually fires.

## Stage 2 — route recall separately from influence

Project Memory's final capstone found routing load-bearing.

Implement explicit task routing before applying authority controls.

At minimum distinguish:

- historical recall,
- present-action/influence.

Historical recall must be able to return legitimate superseded history.

Present-action context must pass temporal validity, scope, frame safety and trust/standing.

Do not use one universal admission path for both.

## Stage 3 — temporal validity

Integrate the existing Project Memory temporal mechanisms.

Support:

- current state,
- superseded state,
- historical state,
- late-arriving knowledge where represented,
- valid-time / known-time queries where the underlying model supports them.

For action context, stale state must not steer current behavior.

For historical recall, stale records remain legitimate history.

## Stage 4 — frame safety

Integrate `ProjectFrame` and `WorkFrame` from Project Memory.

Rules:

- ProjectFrame is durable/versioned configuration.
- WorkFrame is current, evidence-backed work state.
- The current request is not ProjectFrame.
- Uncertain frame establishment must broaden/fallback rather than aggressively exclude decisive evidence.

Persist the frame version used by each context construction.

## Stage 5 — trust and standing

Reuse/adapt `solution/context_frames/trust_policy.py`.

The public decision remains:

```text
admit | deny | quarantine
```

Preserve reason codes.

Trust controls whether memory may influence behavior; it does not declare content true.

Enforce at least:

- project scope,
- revocation,
- provenance integrity,
- temporal validity,
- source authority,
- instruction-like remembered content,
- conflict handling,
- corroboration where Project Memory requires it.

No derived record may launder revoked authority.

## Stage 6 — decisive evidence + provenance

Do not automatically port the largest context assembler.

Project Memory's final result says the simpler decisive-evidence-plus-provenance policy matched the full assembler at lower cost.

Start from that earned final policy.

A context bundle should be small, source-backed and explicit about disagreements.

Do not merge contradictions into fluent consensus.

## Stage 7 — real ContextTrace

Replace the bootstrap hash trace with the actual auditable trace.

For every automatic context bundle record:

- trace ID,
- project identity,
- task route,
- ProjectFrame version,
- WorkFrame evidence,
- query/work signal,
- temporal standpoint,
- retrieval configuration/version,
- lexical/dense/fused/reranked candidates,
- state resolution,
- trust verdict and reason,
- selected decisive evidence,
- dropped candidates and reasons,
- context budget,
- final bundle digest,
- latencies,
- policy versions.

The trace must answer:

- Why did this memory enter?
- Why did that memory stay out?

Expose it through `memory_trace`.

## Stage 8 — unfinished work

Integrate Project Memory open-loop handling.

Do not infer unfinished work from TODO words alone.

Represent expected transitions and their satisfying/cancelling/superseding evidence.

Expose `memory_open_loops`.

Preserve uncertainty when closure search is incomplete.

## Stage 9 — explicit memory actions

Add `memory_remember` only after automatic capture is working.

Explicit memory writes must create attributed events/versions.

Do not mutate canonical past records.

Support correction/supersession/retraction through new records and relationships.

## Stage 10 — evaluation

Do not judge success from retrieval demos.

Build matched OpenCode tasks with:

- same task,
- same model,
- same repository state,
- varied memory condition.

Compare at least:

- no memory,
- strong hybrid RAG,
- remembering policy.

Track:

- task success,
- stale-memory harm,
- current-state correctness,
- historical-state correctness,
- evidence/provenance correctness,
- context tokens,
- latency,
- action differences.

Reuse the Project Memory behavioral/evaluation concepts rather than inventing an unrelated score.

## Required invariants

1. PostgreSQL + pgvector are mandatory.
2. No SQLite fallback.
3. Raw project history is canonical.
4. Derived memory is rebuildable.
5. Context is selected; memory is durable.
6. Projects are isolated by default.
7. Malformed isolation/security configuration fails closed.
8. Historical recall and present influence are routed differently.
9. Every influential memory is traceable to evidence.
10. Simpler mechanisms win ties.

## Immediate next command sequence

After cloning both sibling repositories:

```bash
cd opencode-remembering
bun install
bun run check
bun run build
```

Configure `remembering.json`, then load the local plugin in OpenCode.

First run:

1. call `memory_health`;
2. confirm PostgreSQL and pgvector;
3. implement Stage 1 indexing;
4. seed/index the current repository;
5. call `memory_search`;
6. verify the result points to real Project Memory source IDs;
7. only then proceed to temporal/frame/trust integration.
