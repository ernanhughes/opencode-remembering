# OpenCode Remembering

Standalone OpenCode memory plugin with a bundled Python engine. No
external memory checkout is required.

The mechanisms were earned as experiments in the research companion
[project-memory](https://github.com/ernanhughes/project-memory) (book +
reference implementation). That repository is provenance, not a runtime
dependency: `opencode-remembering` owns its product implementation.

```text
OpenCode
   ↓
opencode-remembering
   ├── TypeScript OpenCode integration (src/)
   ├── bundled remembering engine (engine/remembering/)
   │    ├── baseline: PostgreSQL storage, ingestion, embeddings,
   │    │           hybrid retrieval, routing, temporal admission
   │    └── temporal: event model, log, ordering, reducer,
   │                bitemporal queries, PostgreSQL adapter
   ↓
PostgreSQL + pgvector
```

PostgreSQL and pgvector are **required infrastructure**. There is no
SQLite fallback, no JSON-file vector store, and no Markdown-journal
fallback. The hashing embedder is a deterministic test double, never a
production retrieval model, and the adapter refuses to use it unless
explicitly opted in for local tests.

> Working principle: retrieval finds evidence; later policy stages
> decide whether evidence may influence present action. Historical
> evidence that is superseded may still be perfectly valid for a
> historical recall question. This stage implements strong retrieval
> with provenance — not temporal validity, framing, trust, open loops,
> or consolidation. Those are later stages.

## Status

Working vertical slice:

- OpenCode v2 plugin entry point with five tools.
- Strict configuration with fail-closed isolation.
- Deterministic per-project PostgreSQL schema identity.
- `memory_setup`: fresh-install initialization with zero manual SQL.
- `memory_refresh`: idempotent re-ingestion with change reporting.
- `memory_search`: real hybrid retrieval (FTS + pgvector + RRF) with a
  structured trace proving the dense stage ran.
- `memory_context`: bounded, provenance-bearing bundles.
- Context hook: bounded injection after stable system instructions,
  plus automatic canonical session capture.
- Session transcripts as canonical history ingested by normal refresh.

## Prerequisites

### 1. PostgreSQL + pgvector + a database

You provide:

1. a running PostgreSQL server,
2. an existing database (default name `memory_baseline`, any name works),
3. pgvector installed in that PostgreSQL installation,
4. credentials that may create schemas/tables and enable extensions.

You never create Project Memory tables by hand. `memory_setup` creates
the per-project schema, tables, and indexes. It does **not** create
databases: a missing database yields an actionable `DB_MISSING` error.

If extension creation needs elevated privileges, setup distinguishes
`EXTENSION_UNAVAILABLE` (software missing), `EXTENSION_PERMISSION`
(needs a superuser `CREATE EXTENSION`), and not-enabled-yet.

### 2. Embedding provider

Production path is Ollama `bge-m3` (1024 dimensions):

```powershell
ollama pull bge-m3
```

If the model is missing, health/setup report `EMBEDDING_MODEL_MISSING`
naming the exact model. Nothing is downloaded or substituted silently.

### 3. Python runtime

The plugin's Python bridge needs `psycopg`:

```powershell
python -m pip install -r engine/requirements.txt
```

No other Python packages are required unless explicitly configured
(`sentence-transformers` for that embedding provider or the
cross-encoder reranker).

## Install

```powershell
git clone https://github.com/ernanhughes/opencode-remembering
cd opencode-remembering
bun install
python -m pip install -r engine/requirements.txt
bun run check
bun run build
```

No second repository is cloned. There is no `project-memory`
installation step; the engine ships in `engine/remembering`.

Then add the local plugin path to OpenCode:

```json
{
  "plugins": ["file:///absolute/path/to/opencode-remembering"]
}
```

## Configure

Copy `remembering.example.json` to `~/.config/opencode/remembering.json`
and adjust. Full contract:

```json
{
  "dsn": "postgresql://postgres:<password>@localhost:5432/memory_baseline",
  "python": "python",
  "embedding": {
    "provider": "ollama",
    "model": "bge-m3",
    "host": "http://localhost:11434"
  },
  "retrieval": {
    "mode": "hybrid",
    "lexical_k": 30,
    "dense_k": 30,
    "fusion_k": 60,
    "rerank_k": 8,
    "reranker": "none"
  },
  "context": {
    "auto_inject": true,
    "max_chars": 4000,
    "max_results": 6
  }
}
```

Only `dsn`/`python` need attention in most setups;
everything else has safe defaults. Environment overrides:
`MEMORY_BASELINE_DSN`, `PYTHON`.

Validation is strict: unknown embedding providers, unknown retrieval
modes, unsafe `schema` overrides, and the `hashing` test double all fail
closed with an explicit error. An explicit `schema` overrides the
derived per-project schema and must be a safe SQL identifier.

## First run

```powershell
cd C:\Projects\opencode-remembering
bun run doctor    # explains readiness, points at memory_setup
bun run setup     # initialize schema + ingest this repository
bun run refresh   # re-ingest; reports "nothing changed" when idle
```

Then inside OpenCode:

1. `memory_health` — readiness breakdown (PostgreSQL, pgvector,
   pg_trgm, checkout import, schema, embedding provider/model, stored
   vs configured embedding compatibility, FTS/HNSW indexes, counts).
2. `memory_setup` — initialize this project and index it.
3. `memory_search` — real hybrid results with a retrieval trace.
4. `memory_refresh` — only processes changes afterwards.
5. The context hook begins supplying bounded project evidence.

Outside OpenCode, the same path is available without hand-built JSON:

```powershell
bun src/dev-cli.ts doctor
bun src/dev-cli.ts setup
bun src/dev-cli.ts refresh
bun src/dev-cli.ts search "hybrid retrieval"
bun src/dev-cli.ts context "how does setup work"
bun src/dev-cli.ts context "Where did we discuss routing?" --route recall
bun src/dev-cli.ts route-eval
bun src/dev-cli.ts temporal-import
bun src/dev-cli.ts temporal-state --subject cache.metadata.database
bun src/dev-cli.ts temporal-state --subject cache.metadata.database --temporal-mode valid_at --valid-at 2026-07-20T00:00:00Z
bun src/dev-cli.ts temporal-eval
bun src/dev-cli.ts frame-health
bun src/dev-cli.ts frame-eval
bun src/dev-cli.ts context "Review this." --work-mode explicit --work-type architecture_review --objective "Review the storage architecture."
bun src/dev-cli.ts trust-health
bun src/dev-cli.ts trust-import
bun src/dev-cli.ts trust-eval
bun src/dev-cli.ts context "Fix the migration." --caller-scope coding_agent
bun src/dev-cli.ts selection-eval
bun src/dev-cli.ts context "Implement the migration." --selection full
bun src/dev-cli.ts context "Implement the migration." --selection decisive
bun src/dev-cli.ts trace get <trace-id>
bun src/dev-cli.ts trace find --source docs/adr/017.md
bun src/dev-cli.ts trace verify <trace-id>
bun src/dev-cli.ts trace explain <trace-id> --candidate <chunk-id>
bun src/dev-cli.ts trace replay <trace-id> --kind trust
bun src/dev-cli.ts trace diff <trace-a> <trace-b>
bun src/dev-cli.ts trace-eval
```

## Tools

| Tool | Description |
| ---- | ----------- |
| `memory_health` | Full readiness report incl. temporal section; never prints the DB password. |
| `memory_setup` | Idempotent schema init (retrieval + temporal objects) + ingestion + verification. |
| `memory_refresh` | Idempotent refresh with discovered/indexed/unchanged/added/changed/removed/chunks/embedded/failed, plus automatic temporal import. |
| `memory_search` | Hybrid retrieval with provenance and a stage-by-stage trace (`dense_executed` proves pgvector ran). Route-neutral: finds evidence, never judges its use. |
| `memory_context` | Bounded, **routed, temporally interpreted** bundle (`query`, `route=auto\|recall\|influence`, optional `temporal` standpoint, `trace_id`, `content`, `chars`, `route` + `temporal` blocks, `admission_note`). |
| `memory_state` | Resolved temporal state for one subject (current / valid_at / as_known / bitemporal) with trajectory and provenance. |
| `memory_temporal_import` | Import explicit structured temporal events from `.remembering/temporal/events.jsonl`. Idempotent; malformed lines fail visibly. |

## Routing: recall vs influence

Historical recall and present influence are different optimization
problems. The same record may be valid evidence for "What did we
decide?" while being unsafe guidance for "What should I do now?"

- `memory_search` finds relevant project evidence and is
  retrieval-oriented. It takes no route.
- `memory_context` constructs evidence **for a purpose** and therefore
  accepts/derives a route:
  - `recall` = reconstruct the past. Superseded material stays
    legitimate evidence. Authority must not erase history.
  - `influence` = may steer present action. Retrieval relevance alone
    does not establish currency, authority, or safety.
  - `auto` (default) = deterministic classification; explicit caller
    intent always overrides it. Uncertain queries fall back to
    influence **visibly** (`route_ambiguous: true`), because injected
    context can affect behavior.

Every bundle carries `route`, `route_source` (`explicit` |
`deterministic`), `route_reason`, and `route_ambiguous`, and the trace
records the decision. The context hook injects `route="…"` in the
`<project_memory>` wrapper.

**Stage 2 routing does not enforce temporal validity or trust.** Both
routes use the same strong hybrid retriever; no record is filtered by
route. Later stages attach temporal/frame/trust policy to the
`influence` path. Do not mistake a routed bundle for an authorized one.

Frozen boundary for Stage 3 (temporal): recall preserves historical
candidates and does not suppress superseded evidence merely because
it is no longer current. Influence resolves evidence toward the
applicable current state before it is allowed to steer behaviour.
Recall therefore bypasses *current-state suppression*, not temporal
interpretation itself — "what was true on 11 July" may still need
valid-time/known-time reasoning, while "what did we use before
PostgreSQL" must keep superseded SQLite evidence available.

## Temporal state (Stage 3)

Three time axes stay distinct:

- `event_time` — when the represented event actually happened;
- `recorded_at` — when the memory system learned it (late arrivals
  keep both: learned Aug 15 ≠ known Jul 10);
- `effective_from`/`effective_to` — when the state applies (a decision
  may exist before it takes effect).

Structured temporal events are explicit and evidence-linked. Each line
of `.remembering/temporal/events.jsonl` carries one `EventEnvelope`
(`event_id`, `subject`, `state_key`, `value`, the three time axes,
`supersedes`/`corrects`/`evidence_refs`), validated on import.
No LLM infers subjects or supersession from prose.

Retrieved candidates are annotated, never re-ranked, by temporal
standing: `current`, `historical`, `superseded`, `corrected`,
`planned`, `retracted`, or `not_modelled`. Ordinary prose without
temporal events is `not_modelled` — never guessed current or stale,
never suppressed for lack of semantics. Suppression happens after
retrieval, only on the influence route, only with positive evidence,
and stays visible in the trace with reason codes
(`temporal.superseded_as_current`, `temporal.corrected_as_current`,
`temporal.future_not_effective`, `temporal.retracted_as_current`).

Standpoints: `current` (influence default), `valid_at`, `as_known`,
`bitemporal` (explicit `valid_at`/`known_at`; no natural-language
date guessing). Recall interprets time but preserves history;
influence resolves toward current state.

**Temporal validity does not imply trustworthiness.** A temporally
current record is not automatically authoritative, safe, or permitted
to steer behaviour. That is the next stage.

## Safe framing (Stage 4)

Two separate objects:

- **ProjectFrame** — durable, versioned project configuration in
  `.remembering/project-frame.json` (never the current request):
  project id, version, purpose, objectives, constraints, declared
  work types with match terms, evidence preferences per work type.
  Malformed files disable framing visibly; a frame claiming another
  project fails closed (`FRAME_PROJECT_MISMATCH`).
- **WorkFrame** — ephemeral, evidence-backed current work state:
  objective (observed signal text, never invented), work type, and
  per-field provenance listing the WorkSignal IDs behind each field.

WorkSignals are current observations only (latest user request,
agent task, newest tool result, test failure) — never the whole
history. Establishment is deterministic, with no scalar confidence:
`declared`, `corroborated` (independent signals agree),
`inferred` (weak evidence), `conflicting`, `stale` (newer direct
signal contradicts the prior frame), `unknown`.

Control follows strength:

```text
declared / corroborated → HARD_FRAME (order, scope, opt-in exclusion)
inferred                → SOFT_FRAME (may add and reorder, never erase)
conflicting / stale / unknown → QUERY_ONLY (Stage 3 path untouched)
```

> **A weak frame may assist retrieval; it may not erase the strong
> baseline.**

> **Framing applies primarily to present-action influence, not as a
> universal filter over historical recall.**

> **Frame relevance does not imply trust or authority. Stage 5
> handles standing.**

## Trust and standing (Stage 5)

Stages 1–4 establish that evidence is retrievable, correctly routed,
temporally applicable, and fits the current work. Stage 5 asks whether
it has **standing to steer behaviour**:

```text
relevant ≠ true ≠ current ≠ authoritative ≠ permitted to influence
```

Trust is permission, not truth. A denied source may be accurate; a
revoked source stays searchable history. Verdicts are `admit`, `deny`,
`quarantine` — never `true/false`, never a scalar score, never a
hidden `malicious` flag.

- **Explicit policy** in `.remembering/trust/policy.json`: source
  classes (`authoritative`/`informational`/`untrusted` with
  may-inform/may-direct), deterministic path rules (most-specific
  wins), roles, caller restrictions. Absence uses a conservative
  builtin; malformed files fail visibly.
- **Append-only standing events** (`.remembering/trust/events.jsonl`
  → `<schema>.standing_events`): revocation is standing, not
  content — distinct from temporal retraction. Revocation inherits
  through derivation closure, so restatements cannot launder a
  revoked source.
- **Deterministic instruction screen** (versioned): bypass/skip/ignore
  patterns quarantine unverified directives; requirement-worded rules
  ("require a passing rollback test") do not trip it. Refuted
  directives are denied; independently corroborated ones (disjoint
  root lineages, same claim key) may be admitted.
- **Informing vs directing**: benchmarks and test output inform;
  decisions and directives need standing or corroboration.
  Preferences never guide action. Corroborated conflicts quarantine
  both sides rather than rank-picking.
- **Recall and search stay broad**: denied/quarantined evidence
  remains searchable, inspectable, and trace-visible — excluded from
  automatic influence injection only.

> **A temporally current, correctly framed record is not automatically
> trustworthy. Marked visibility is not claimed as injection-proof.**

## Decisive selection (Stage 6)

Admission is permission; selection is necessity. Of the admitted
candidates, the smallest provenance-bearing set is selected:

- **Decisive**: current authoritative decisions/state, active
  project constraints, negative evidence that blocks a bad action,
  material disagreements (both sides, never merged).
- **Supporting**: independent benchmarks/tests, frame-preferred
  evidence, grounding provenance pulled in to license derived claims.
- **Contextual** loses to the above; **redundant** echoes
  (shared claim keys, shared derivation roots, identical spans)
  collapse to one representative.

Disagreement is rendered in separate sections, never merged into
fluent consensus. No LLM selector, no scalar importance score, no
summarization. Recall preserves broadly instead of compressing.

## Durable ContextTrace (Stage 7)

Every context construction persists an immutable content-addressed
trace (`ctx_<sha256>`) **before** the bundle is returned. Influence
refuses injection when persistence fails (`TRACE_PERSIST_FAILED`);
recall degrades to an unpersisted trace rather than failing.

- **Candidate lifecycle**: every retrieved candidate carries its
  staged path (retrieval ranks/paths, temporal status, frame
  eligibility, trust verdict, selection disposition) plus a terminal
  stage (`FINAL_SELECTED`, `TEMPORAL_SUPPRESSED`, `FRAME_EXCLUDED`,
  `TRUST_DENIED`, `TRUST_QUARANTINED`, `SELECTION_REDUNDANT`,
  `SELECTION_LOW_VALUE`, `SELECTION_BUDGET`). Stages that never ran
  read `not_reached` — never fabricated.
- **Digests**: trace ID (semantic identity, timings excluded),
  bundle digest (exact rendered content), candidate-pool digest
  (order-independent), input digest. Tampering is detected, not
  repaired.
- **Lookup**: `memory_trace` supports exact fetch, filtered search
  (source/chunk/route/policy/terminal stage), deterministic candidate
  explanation, integrity verification, bounded trust/selection
  replay, and structured diff. Counterfactual replays get their own
  IDs, never mutate the original, and state their frozen evidence
  boundary.
- **Isolation**: per-project trace tables; cross-project fetch fails
  closed. Traces are audit state, never re-ingested as memory; no
  secrets are persisted; retention defaults to indefinite.

> A ContextTrace is the preserved state of the context decision that
> happened before the model received memory — not an explanation
> generated after the fact. It cannot prove what the model did with
> the bundle, only what the pipeline decided.
`memory_context` accepts `selection: {mode: decisive|full}` (`full`
= admitted-full baseline for comparison); the trace records groups,
dispositions, budget before/after, and compression.

## Project isolation

Unless `schema` is configured explicitly, the plugin derives:

```text
remembering_<sha256(realpath(project))[:12]>
```

Windows paths are canonicalized (realpath + lowercase) so the same
repository always resolves to the same schema, and different
repositories never share one. Setup also records the canonical project
path in `<schema>.meta`; a schema already claimed by another project
fails closed with `SCHEMA_MISMATCH` instead of mixing histories.

## Session capture

The context hook automatically merges unseen OpenCode messages into
`<project>/.remembering/sessions/<sessionId>.json` — canonical facts
(session/message/tool-call/tool-result identity, role, content, agent,
model, observed timestamp) recorded verbatim, never summarized, never
rewritten, never duplicated. The next `memory_setup`/`memory_refresh`
ingests changed transcripts as ordinary versioned sources through the
standard ingestion path. Capture needs no database, so history survives
PostgreSQL outages; embedding happens at refresh time.

Derived summaries are not canonical history and are never written by
this path.

## Failure semantics

- Startup: an unavailable backend logs a warning and the plugin still
  loads. Only isolation/security violations (`SCHEMA_MISMATCH`,
  `CONFIG_INVALID`) abort startup.
- Context hook: capture errors, empty queries, uninitialized stores,
  and unhealthy backends all degrade to "no injection". A failed
  memory lookup never breaks an unrelated session.
- Search on an uninitialized schema returns an `indexed: false`
  message pointing at `memory_setup`, not an error.

## Tests

```powershell
bun run check          # typecheck + bun tests
python -m pytest engine/tests/ bridge/ -v   # engine units + bridge units, always green
```

With PostgreSQL (and Ollama for the full suite):

```powershell
$env:MEMORY_BASELINE_DSN = "postgresql://postgres:<pw>@localhost:5432/memory_baseline"
python -m pytest engine/tests/ bridge/ -v
```

No test reads any repository outside `opencode-remembering`.

## Boundary with the research repository

`project-memory` (book + experiments + reference implementation) is
where these mechanisms were earned. It is not installed, imported, or
consulted at runtime. Stage 4+ work belongs here: consult the research
repo, port an already-earned mechanism with attribution, and implement
the product in `engine/remembering`.

## Development principle

Do not add a second memory engine, a second database, or silent
fallbacks. When a missing capability is needed, add the smallest
explicit mechanism with a trace, a reason code, and a test.

The final target remains:

```text
canonical project history
→ strong retrieval
→ routing (recall vs influence)
→ temporal/evidence resolution
→ current work frame
→ trust/admission
→ bounded context
→ OpenCode behavior
→ evaluation
```
