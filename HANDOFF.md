# OpenCode Remembering — maintainer handoff

> Current state: **Stages 1–9 implemented, verified, and frozen.**
> Stage 10 is matched behavioral evaluation, not another memory mechanism.
> Do not add memory mechanisms. Do not begin Stage 10 unprompted.

Repository: `https://github.com/ernanhughes/opencode-remembering` (standalone product).
Research reference only (never a runtime dependency, never modify): `project-memory` and related research repos.

## Frozen architecture

```text
                    EXPLICIT MEMORY ACTIONS
                            ↓
                       WRITE POLICY
                            ↓
                     CANONICAL HISTORY
                            ↓
                     HYBRID RETRIEVAL
                            ↓
                   RECALL / INFLUENCE
                            ↓
                   TEMPORAL RESOLUTION
                            ↓
                     SAFE WORK FRAME
                            ↓
                    TRUST / STANDING
                            ↓
                  DECISIVE SELECTION
                            ↓
                          BUDGET
                            ↓
                   DURABLE CONTEXTTRACE
                            ↓
                         OPENCODE

alongside: CANONICAL EVENTS → OPEN-LOOP PROJECTOR →
OPEN / COMPLETED / CANCELLED / SUPERSEDED / UNCERTAIN →
relevant derived candidates through the normal pipeline.
```

## Frozen invariants

1. PostgreSQL + pgvector mandatory; no SQLite/JSON fallbacks; no silent fallbacks, model substitution, or migrations.
2. History is canonical; derived state is rebuildable; traces are audit evidence, never re-ingested memory.
3. Recall preserves history; influence is gated. Routing never filters by itself.
4. Temporal validity ≠ trustworthiness; framing relevance ≠ authority; trust verdict ≠ truth; admission ≠ selection.
5. Revocation inherits through derivation closure; lifecycle relationships (`corrects`/`supersedes`/`retracts`) never enter derivation lineage.
6. Writes are append-only events, never edits: remember/correct/supersede create records, retraction never deletes, no `memory_delete` exists.
7. `WRITE_ALLOWED ≠ TRUST_ADMITTED`: the write policy caps standing; Stage 5 is final for influence.
8. Successful writes are immediately retrievable; failed writes leave no partial state; idempotent retries collapse, conflicts fail visibly.
9. Historical write authorization is never retroactively rewritten.
10. No LLM classifiers or scalar scores anywhere in policy paths; every gate is deterministic and reason-coded.

## Test and evaluation spine (verified)

```text
routing:    21/21            temporal:   16/16
framing:    10/10            trust:      17/17 (+ T0–FULL ladder)
selection:  18/18            trace:      11/11 engine + A–W bridge
loops:      13/13 engine + integration
writes:     30 categories, 44/44 checks + 24 PostgreSQL tests
Bun: 47/47   Engine: 16/16   Bridge: 123/123 (139 Python total)
Build: green   Package: 78 real files   Packed evals: 8/8 green (scrubbed C:/)
```

Live acceptance (PG + pgvector + bge-m3): remember/correct/supersede/retract sequence with trace demonstration, plus the builtin poison negative control (persisted as untrusted history, `deny.untrusted_source` on influence).

## Stage history (summarized)

- **Stage 1**: real indexing — setup/refresh, hybrid FTS + pgvector + RRF, session capture, bounded provenance bundles.
- **Stage 2**: recall/influence routing (21/21); recall preserves superseded history.
- **Stage 3**: bitemporal temporal engine (16/16); `memory_state`, standpoint queries; validity ≠ trust.
- **Stage 3.5**: standalone extraction — bundled `engine/remembering`, no `project-memory` runtime; stale root settings fail closed.
- **Stage 4**: ProjectFrame/WorkFrame, six establishment classes, HARD/SOFT/QUERY_ONLY (10/10); weak frames never erase the baseline.
- **Stage 5**: admit/deny/quarantine gate, standing events, instruction screening, corroboration, conflict quarantine (17/17).
- **Stage 6**: decisive/supporting/contextual/redundant selection with provenance closure and budget (18/18); no summarization.
- **Stage 7**: immutable content-addressed ContextTrace persisted before influence; lookup/explain/verify/replay/diff (11/11 + A–W).
- **Stage 8**: open loops with evidence-tested closure, bitemporal standpoints, derived-candidate integration (13/13 + integration); TODO text never creates loops.
- **Stage 9**: `memory_remember` (remember/correct/supersede/retract), deterministic write gate, two-key standing, immediate indexing, rebuildable projections (30 categories, 44/44 + 24 PG).

## Working safely

- Keep per-stage contracts green and separate; never merge evaluations into one score.
- Operational exclusions live in `engine/remembering/baseline/ingest.py` (`SKIP_PATHS`) plus the pipeline prune guard for `memory://` sources. Note the honest limitation: pre-existing operator policy files (trust policy, project frame) still ingest as ordinary sources.
- Write-policy, trust-policy, and schema changes must fail closed with named codes; reads degrade visibly, never silently.
- Dev CLIs: `bun run check/build`, `python -m pytest engine/tests/ bridge/`, per-area `*-eval` scripts, packed verification from outside the checkout.
- Local services: `MEMORY_BASELINE_DSN` (default `postgresql://...@localhost:5434/memory`), Ollama `bge-m3` at `localhost:11434`, hashing embedder only with `REMEMBERING_ALLOW_TEST_EMBEDDINGS=1`.

## Recommended Stage 10 (not implemented)

Matched behavioral evaluation: M0 (no memory) vs M1 (strong hybrid RAG) vs M2 (full remembering) on identical tasks, measuring task success, stale-memory harm, current/historical-state correctness, unsafe-memory influence, unfinished-work and provenance correctness, context size, latency, and action differences — separately, with no composite score.

## Stage 3 — Native TypeScript memory semantics (canonical engine)

Status: native engine complete; OpenCode still uses the legacy bridge until Stage 4 cutover.

Native pipeline (all TypeScript, no subprocess):

retrieval -> temporal -> frame -> trust/standing -> selection/budget -> ContextTrace -> open loops + explicit writes

New modules under src/engine/: temporal/{model,reducer,store,service},
frame/{model,service}, trust/{model,policy,standing,store}, selection/{model,service},
trace/{model,store,service}, loops/{model,store}, write/{model,policy,store,service},
context.ts (route + staged assembly). RememberingEngine exposes temporalImport, state,
frameEstablish, trustDecisions, trace*, loop*, remember/recordShow/writeHistory/writeRebuild,
context, nativeDoctor. CLI: bun src/native-cli.ts <doctor|setup|refresh|search|context|state|
temporal-import|frame-health|trust-health|trace|loop-health|loop-list|loop-show|loop-history|
remember|write-health|write-history|write-rebuild|record-show>.

Invariants: append-only histories; recall preserves revoked/superseded evidence while
influence denies it; retrieved text is data (instruction screening quarantines, never executes);
memory:// sources survive refresh pruning; zero events/loops/traces is healthy, not failure.
sentence-transformers/cross-encoder remain fail-closed (no legacy runtime fallback).

Validation: 114 tests pass, tsc clean, build clean, git diff --check clean.
ProjectMemoryClient NOT cut over. bridge/ + engine/remembering/ retained as legacy, unmodified.

## Stage 4 — Native cutover and legacy removal (complete)

ProjectMemoryClient now delegates every method to RememberingEngine; no subprocess,
no JSON protocol, no interpreter lookup. config no longer carries python/bridgePath
(stale keys are simply ignored); hashing provider allowed under
REMEMBERING_ALLOW_TEST_EMBEDDINGS=1. Deleted: bridge/, engine/ (incl.
requirements.txt), .pytest_cache, bridge-tests script. Package ships 5 files
(dist + docs + example config); packed install verified in a scrubbed directory.
Regression guard widened: src/no-python-runtime.test.ts scans all of src/ + index.ts
for subprocess/legacy tokens. Eval commands run native fixture evaluations
(src/engine/evaluations.ts). Session capture is file-based and DB-independent.

Validation: 121 tests pass, tsc clean, build clean, git diff --check clean.
Migration Python -> TypeScript is complete. Next: product hardening (install,
configuration, live project use, performance, memory behavior).

## Health/readiness contract fix

Root cause: doctorBaseline() built the report with ok:false and never recomputed
it, so a healthy native runtime still reported ok=false (CLI exit 1).

Fix: src/engine/readiness.ts (pure readiness calculus) + doctorBaseline() now sets
ok/readiness/message from probed state; nativeDoctor() folds subsystem sections
(trust/write policy validity, store readiness, loop projection errors) into a
whole-product readiness. Fresh DB returns ok=false/NOT_INITIALIZED without throwing;
SCHEMA_MISMATCH and DIMENSION_MISMATCH stay fail-closed throws. Added readiness
codes: HEALTHY, NOT_INITIALIZED, BACKEND_UNREACHABLE, EXTENSION_MISSING,
EMBEDDING_UNAVAILABLE, DIMENSION_MISMATCH, SCHEMA_MISMATCH, POLICY_INVALID,
SUBSYSTEM_FAILURE. DSN redaction now preserves user/host and redacts only the
password. Self-check shows database endpoint + readiness code.

Note: a stale session-level MEMORY_BASELINE_DSN (port 5434) can shadow the
machine-level value (port 5432); env precedence is unchanged by design.
