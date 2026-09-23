# OpenCode Remembering

OpenCode integration for [Project Memory](https://github.com/ernanhughes/project-memory).

This repository is intentionally a **thin plugin**, not a second memory implementation. The memory mechanisms live in `project-memory`; this repository connects OpenCode sessions to them.

## Architectural rule

```text
OpenCode
   ↓
opencode-remembering (TypeScript integration)
   ↓
local Python bridge
   ↓
ernanhughes/project-memory
   ↓
PostgreSQL + pgvector
```

PostgreSQL and pgvector are **required infrastructure**. There is no SQLite fallback and no in-plugin vector store.

## Status

Bootstrap / v0.0.1.

Implemented in this first slice:

- OpenCode v2 plugin entry point.
- Hard startup check for:
  - a local `project-memory` checkout,
  - Python + `psycopg`,
  - reachable PostgreSQL,
  - installed `vector` extension.
- Deterministic per-project PostgreSQL schema identity, preventing two repositories from accidentally sharing the same retrieval index.
- `memory_health` tool.
- `memory_search` tool backed by `project-memory`'s PostgreSQL full-text store.
- `memory_context` tool for a bounded manual bundle.
- Optional bounded context injection through the OpenCode context hook.
- Python bridge kept deliberately small so a later stable Project Memory service/API can replace it without changing the OpenCode-facing contract.

Not yet implemented here:

- automatic ingestion of OpenCode sessions,
- automatic repository refresh,
- dense pgvector retrieval and reciprocal-rank fusion in the bridge,
- temporal resolution,
- ProjectFrame / WorkFrame establishment,
- trust/admission,
- full ContextTrace persistence,
- open-loop queries.

Those already have reference implementations or experimental machinery in `project-memory`; see [HANDOFF.md](HANDOFF.md).

## Prerequisites

### 1. Local repositories

A convenient layout is:

```text
C:/Projects/
├── project-memory/
└── opencode-remembering/
```

The plugin resolves Project Memory in this order:

1. `PROJECT_MEMORY_ROOT`
2. `project_memory_root` in `~/.config/opencode/remembering.json`
3. a sibling `../project-memory` directory relative to the repository currently open in OpenCode

### 2. PostgreSQL + pgvector

PostgreSQL must already be installed and running, and the target database must have pgvector enabled.

Example:

```sql
CREATE DATABASE memory_baseline;
\c memory_baseline
CREATE EXTENSION IF NOT EXISTS vector;
```

The default DSN follows Project Memory:

```text
postgresql://postgres:postgres@localhost:5434/memory_baseline
```

Override it with `MEMORY_BASELINE_DSN` or `dsn` in the plugin config.

There is intentionally no Docker/PostgreSQL bootstrap inside this repository.

### 3. Python environment

The Python used by the plugin must be able to import `psycopg`.

Set `python` in the plugin config if `python` is not the correct executable.

## Install for local development

```bash
git clone https://github.com/ernanhughes/project-memory
git clone https://github.com/ernanhughes/opencode-remembering
cd opencode-remembering
bun install
bun run build
bun test
```

Then add the local plugin path to OpenCode:

```json
{
  "plugins": [
    "file:///absolute/path/to/opencode-remembering"
  ]
}
```

Example `~/.config/opencode/remembering.json`:

```json
{
  "project_memory_root": "C:/Projects/project-memory",
  "dsn": "postgresql://postgres:postgres@localhost:5434/memory_baseline",
  "python": "python",
  "context": {
    "auto_inject": true,
    "max_chars": 4000,
    "max_results": 6
  }
}
```

Copy [remembering.example.json](remembering.example.json) if useful.

## Tools

### `memory_health`

Checks the Project Memory checkout, PostgreSQL connection, pgvector extension, and whether this repository's isolated Project Memory schema has been initialised.

### `memory_search`

Runs lexical full-text retrieval through Project Memory's `memory_baseline.storage.Store`.

This first slice intentionally does **not** pretend lexical search is the final remembering system. Hybrid pgvector + lexical retrieval is the next milestone.

### `memory_context`

Builds a small bounded retrieval bundle for a query. This is a bootstrap integration seam, not the final Project Memory ContextBundle/ContextTrace implementation.

## Project isolation

Unless `schema` is configured explicitly, the plugin derives a schema from the canonical repository path:

```text
remembering_<sha256(realpath(project))[:12]>
```

That means two OpenCode repositories do not silently query the same memory index.

A configured `schema` overrides this behavior and should therefore be used deliberately.

## Fail-closed behavior

Startup fails when:

- Project Memory cannot be located/imported,
- PostgreSQL cannot be reached,
- pgvector is not installed in the configured database.

Search does not silently fall back to files, SQLite, or a global journal.

If the per-project schema has not yet been populated, health reports `indexed=false` and search returns a clear initialization message.

## Development principle

Do not reimplement Project Memory inside this repository.

When a missing capability is needed, prefer one of:

1. expose a stable API from `project-memory`,
2. adapt an existing Project Memory module,
3. add a narrowly-scoped bridge operation here as a temporary integration seam.

The final target remains:

```text
canonical project history
→ strong retrieval
→ temporal/evidence resolution
→ current work frame
→ trust/admission
→ bounded context
→ OpenCode behavior
→ evaluation
```
