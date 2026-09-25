-- remembering-http-v1.sql
-- Versioned PostgreSQL RPC contract for the opencode-remembering HTTP backend.
-- Install once per database, then expose via PostgREST / Supabase-style gateway.
--
--   psql "$DATABASE_URL" -f sql/remembering-http-v1.sql
--
-- Contract rule (enforced by src/engine/storage/http/contract.test.ts):
-- every PostgreSQL function argument name MUST exactly match the JSON key the
-- TypeScript client sends, because PostgREST matches request keys to argument
-- names. Do not add `p_` prefixes. Reserved-word arguments are quoted but keep
-- their exact key spelling ("timestamp", "query", "key", "state", "action").
--
-- Design:
-- - One RPC function per semantic storage operation (no arbitrary SQL over HTTP).
-- - Every function takes an explicit schema name and validates it as a safe
--   identifier; cross-project access is refused inside the database.
-- - Requires: pgvector, pg_trgm. Idempotent (CREATE OR REPLACE / IF NOT EXISTS).
-- - Grant EXECUTE only to the dedicated api role, e.g.:
--     GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO remembering_api;
--   Never grant the api role CREATE / DDL outside these functions.
-- - Response key spellings match what Http*Store expects, including camelCase
--   envelope keys (eventId, sourceId, ...) for temporal/standing/trace/write.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Helpers (not exposed over HTTP directly)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_assert_schema(schema text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF schema IS NULL OR schema !~ '^[A-Za-z_][A-Za-z0-9_$]*$'
     OR char_length(schema) > 63
     OR lower(schema) LIKE 'pg\_%' THEN
    RAISE EXCEPTION 'CONFIG_INVALID: refusing unsafe schema %', schema;
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Health / setup
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_health(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v_chunks regclass;
BEGIN
  PERFORM remembering_assert_schema(schema);
  SELECT to_regclass(schema || '.chunks') INTO v_chunks;
  RETURN jsonb_build_object('ok', true, 'initialised', v_chunks IS NOT NULL, 'schema', schema);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_initialise(schema text, embedding_dim int)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  IF embedding_dim IS NULL OR embedding_dim < 1 OR embedding_dim > 65535 THEN
    RAISE EXCEPTION 'CONFIG_INVALID: bad embedding dimension %', embedding_dim;
  END IF;
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.sources (source_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL, content_hash TEXT NOT NULL, timestamp TEXT, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())', schema);
  -- NOTE: vector dimension cannot use a parameter; embedding_dim is validated above.
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES %I.sources(source_id) ON DELETE CASCADE, ordinal INT NOT NULL, text TEXT NOT NULL, section TEXT, char_start INT NOT NULL, char_end INT NOT NULL, content_hash TEXT NOT NULL, chunker TEXT NOT NULL, embedding_version TEXT NOT NULL, embedding vector(%s) NOT NULL, tsv TSVECTOR NOT NULL)', schema, schema, embedding_dim);
  EXECUTE format('CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON %I.chunks USING GIN (tsv)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.temporal_events (event_id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, value TEXT, event_time TIMESTAMPTZ NOT NULL, known_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, supersedes TEXT, reason TEXT, received_at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.standing_events (event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, transition TEXT NOT NULL, level TEXT, reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.context_traces (trace_id TEXT PRIMARY KEY, query TEXT NOT NULL, route TEXT NOT NULL, work_type TEXT, created_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL, digest TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.open_loop_events (event_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, transition_kind TEXT NOT NULL, from_state TEXT, to_state TEXT, expected JSONB NOT NULL DEFAULT ''{}'', evidence_refs JSONB NOT NULL DEFAULT ''[]'', closure JSONB NOT NULL DEFAULT ''[]'', reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_records (record_id TEXT PRIMARY KEY, content TEXT NOT NULL, role TEXT NOT NULL, source_id TEXT NOT NULL, lineage_root TEXT NOT NULL, standing_ceiling TEXT NOT NULL, state TEXT NOT NULL, caller_scope TEXT NOT NULL, origin TEXT NOT NULL, evidence_refs JSONB NOT NULL DEFAULT ''[]'', event_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_actions (action_id TEXT PRIMARY KEY, action TEXT NOT NULL, record_id TEXT, target_record_id TEXT, reason TEXT NOT NULL DEFAULT '''', caller_scope TEXT NOT NULL, origin TEXT NOT NULL, idempotency_key TEXT, relation JSONB, created_at TIMESTAMPTZ NOT NULL)', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

-- Baseline ports ------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_check_dimension(schema text, embedding_dim int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE actual int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  SELECT atttypmod INTO actual FROM pg_attribute
    JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
    JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
   WHERE pg_namespace.nspname = schema AND pg_class.relname = 'chunks' AND pg_attribute.attname = 'embedding';
  IF actual IS NOT NULL AND actual <> -1 AND actual <> embedding_dim THEN
    RAISE EXCEPTION 'DIMENSION_MISMATCH: table holds %, requested %', actual, embedding_dim;
  END IF;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_upsert_source(schema text, source_id text, artifact_type text, content_hash text, "timestamp" text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE changed boolean := false;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('INSERT INTO %I.sources (source_id, artifact_type, content_hash, timestamp) VALUES ($1,$2,$3,$4) ON CONFLICT (source_id) DO UPDATE SET artifact_type=EXCLUDED.artifact_type, content_hash=EXCLUDED.content_hash, timestamp=EXCLUDED.timestamp, ingested_at=now() RETURNING (xmax = 0)', schema)
    USING source_id, artifact_type, content_hash, "timestamp" INTO changed;
  RETURN jsonb_build_object('changed', coalesce(changed, true));
END;
$$;

CREATE OR REPLACE FUNCTION remembering_known_sources(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(jsonb_build_object(''source_id'', source_id, ''content_hash'', content_hash)), ''[]''::jsonb) FROM %I.sources', schema) INTO rows;
  RETURN jsonb_build_object('sources', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_remove_source(schema text, source_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('DELETE FROM %I.sources WHERE source_id = $1', schema) USING source_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN jsonb_build_object('removed', n);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_replace_chunks(schema text, source_id text, chunks jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE item jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('DELETE FROM %I.chunks WHERE source_id = $1', schema) USING source_id;
  FOR item IN SELECT * FROM jsonb_array_elements(coalesce(chunks, '[]'::jsonb)) LOOP
    EXECUTE format('INSERT INTO %I.chunks (chunk_id, source_id, ordinal, text, section, char_start, char_end, content_hash, chunker, embedding_version, embedding, tsv) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector, to_tsvector(''english'', $4))', schema)
      USING item->>'chunk_id', item->>'source_id', (item->>'ordinal')::int, item->>'text',
            item->>'section', (item->>'char_start')::int, (item->>'char_end')::int,
            item->>'content_hash', item->>'chunker', item->>'embedding_version', item->>'embedding';
  END LOOP;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_lexical_search(schema text, "query" text, k int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE items jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format($f$SELECT coalesce(jsonb_agg(t ORDER BY rank), '[]'::jsonb) FROM (SELECT chunk_id, source_id, text, section, ts_rank_cd(tsv, to_tsquery('english', $1)) AS score, row_number() OVER (ORDER BY ts_rank_cd(tsv, to_tsquery('english', $1)) DESC, chunk_id) AS rank FROM %I.chunks WHERE tsv @@ to_tsquery('english', $1) ORDER BY 5 DESC, chunk_id LIMIT $2) t$f$, schema)
    USING "query", k INTO items;
  RETURN jsonb_build_object('items', items);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_dense_search(schema text, query_vector text, k int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE items jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY rank), ''[]''::jsonb) FROM (SELECT chunk_id, source_id, text, section, 1 - (embedding <=> $1::vector) AS score, row_number() OVER (ORDER BY embedding <=> $1::vector, chunk_id) AS rank FROM %I.chunks ORDER BY embedding <=> $1::vector, chunk_id LIMIT $2) t', schema)
    USING query_vector, k INTO items;
  RETURN jsonb_build_object('items', items);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_ensure_hnsw(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  BEGIN
    EXECUTE format('CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON %I.chunks USING hnsw (embedding vector_cosine_ops)', schema);
    RETURN jsonb_build_object('created', true);
  EXCEPTION WHEN program_limit_exceeded THEN
    RETURN jsonb_build_object('created', false);
  END;
END;
$$;

CREATE OR REPLACE FUNCTION remembering_stats(schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE out jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT jsonb_build_object(''sources'', (SELECT count(*) FROM %I.sources), ''chunks'', (SELECT count(*) FROM %I.chunks))', schema, schema) INTO out;
  RETURN out;
END;
$$;

CREATE OR REPLACE FUNCTION remembering_orphans(schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.chunks c LEFT JOIN %I.sources s ON c.source_id = s.source_id WHERE s.source_id IS NULL', schema, schema) INTO n;
  RETURN jsonb_build_object('orphans', n);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_embedding_versions(schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(DISTINCT embedding_version), ''[]''::jsonb) FROM %I.chunks', schema) INTO v;
  RETURN jsonb_build_object('versions', v);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_is_initialised(schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE r regclass;
BEGIN
  PERFORM remembering_assert_schema(schema);
  SELECT to_regclass(schema || '.chunks') INTO r;
  RETURN jsonb_build_object('initialised', r IS NOT NULL);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_read_meta(schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE m jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  BEGIN
    EXECUTE format('SELECT coalesce(jsonb_object_agg(key, value), ''{}''::jsonb) FROM %I.meta', schema) INTO m;
  EXCEPTION WHEN undefined_table THEN
    m := '{}'::jsonb;
  END;
  RETURN jsonb_build_object('meta', m);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_meta(schema text, entries jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE k text; v text;
BEGIN
  PERFORM remembering_assert_schema(schema);
  FOR k, v IN SELECT key, value FROM jsonb_each_text(entries) LOOP
    EXECUTE format('INSERT INTO %I.meta (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING', schema) USING k, v;
  END LOOP;
  RETURN jsonb_build_object('ok', true);
END;
$$;

-- Temporal event store ------------------------------------------------------
-- Envelope shape (camelCase, mirrors TemporalEnvelope): eventId, subject,
-- kind, value, eventTime, knownTime?, effectiveFrom?, supersedes?, reason?,
-- receivedAt. knownTime defaults to receivedAt; effectiveFrom to eventTime.

CREATE OR REPLACE FUNCTION remembering_temporal_initialise(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.temporal_events (event_id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, value TEXT, event_time TIMESTAMPTZ NOT NULL, known_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, supersedes TEXT, reason TEXT, received_at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE INDEX IF NOT EXISTS temporal_events_subject_idx ON %I.temporal_events (subject)', schema);
  EXECUTE format('INSERT INTO %I.meta (key, value) VALUES (''temporal.store_version'', ''temporal-store-v0.1''), (''temporal.event_schema'', ''temporal-event-v0.1'') ON CONFLICT (key) DO NOTHING', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_temporal_append(schema text, envelope jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.temporal_events WHERE event_id = $1', schema) USING envelope->>'eventId' INTO n;
  IF n > 0 THEN RETURN jsonb_build_object('duplicate', true); END IF;
  EXECUTE format('INSERT INTO %I.temporal_events (event_id, subject, kind, value, event_time, known_time, effective_from, supersedes, reason, received_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)', schema)
    USING envelope->>'eventId', envelope->>'subject', envelope->>'kind', envelope->>'value',
          (envelope->>'eventTime')::timestamptz,
          coalesce((envelope->>'knownTime'), (envelope->>'receivedAt'))::timestamptz,
          coalesce((envelope->>'effectiveFrom'), (envelope->>'eventTime'))::timestamptz,
          envelope->>'supersedes', envelope->>'reason', (envelope->>'receivedAt')::timestamptz;
  RETURN jsonb_build_object('duplicate', false);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_temporal_list(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY known_time, event_id), ''[]''::jsonb) FROM (SELECT event_id AS "eventId", subject, kind, value, event_time AS "eventTime", known_time AS "knownTime", effective_from AS "effectiveFrom", supersedes, reason, received_at AS "receivedAt" FROM %I.temporal_events ORDER BY known_time, event_id) t', schema) INTO rows;
  RETURN jsonb_build_object('events', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_temporal_count(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.temporal_events', schema) INTO n;
  RETURN jsonb_build_object('count', n);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_temporal_subjects(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(subject ORDER BY subject), ''[]''::jsonb) FROM (SELECT DISTINCT subject FROM %I.temporal_events) s', schema) INTO rows;
  RETURN jsonb_build_object('subjects', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_temporal_versions(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE sv text; ev text;
BEGIN
  PERFORM remembering_assert_schema(schema);
  BEGIN
    EXECUTE format('SELECT value FROM %I.meta WHERE key = ''temporal.store_version''', schema) INTO sv;
    EXECUTE format('SELECT value FROM %I.meta WHERE key = ''temporal.event_schema''', schema) INTO ev;
  EXCEPTION WHEN undefined_table THEN
    sv := NULL; ev := NULL;
  END;
  RETURN jsonb_build_object('storeVersion', sv, 'eventSchemaVersion', ev);
END;
$$;

-- Standing event store ------------------------------------------------------
-- Event shape (camelCase, mirrors StandingEvent): eventId, sourceId,
-- transition, level?, reason, at.

CREATE OR REPLACE FUNCTION remembering_standing_initialise(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.standing_events (event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, transition TEXT NOT NULL, level TEXT, reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('INSERT INTO %I.meta (key, value) VALUES (''standing.store_version'', ''standing-store-v0.1'') ON CONFLICT (key) DO NOTHING', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_standing_append(schema text, event jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.standing_events WHERE event_id = $1', schema) USING event->>'eventId' INTO n;
  IF n > 0 THEN RETURN jsonb_build_object('duplicate', true); END IF;
  EXECUTE format('INSERT INTO %I.standing_events (event_id, source_id, transition, level, reason, at) VALUES ($1,$2,$3,$4,$5,$6)', schema)
    USING event->>'eventId', event->>'sourceId', event->>'transition',
          event->>'level', coalesce(event->>'reason', ''), (event->>'at')::timestamptz;
  RETURN jsonb_build_object('duplicate', false);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_standing_list(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY at, event_id), ''[]''::jsonb) FROM (SELECT event_id AS "eventId", source_id AS "sourceId", transition, level, reason, at FROM %I.standing_events ORDER BY at, event_id) t', schema) INTO rows;
  RETURN jsonb_build_object('events', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_standing_count(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.standing_events', schema) INTO n;
  RETURN jsonb_build_object('count', n);
END;
$$;

-- ContextTrace store ---------------------------------------------------------
-- Trace shape (camelCase, mirrors ContextTraceRecord): traceId, query, route,
-- workType, createdAt, candidates, policies, budget, digest, replayOf.

CREATE OR REPLACE FUNCTION remembering_trace_initialise(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.context_traces (trace_id TEXT PRIMARY KEY, query TEXT NOT NULL, route TEXT NOT NULL, work_type TEXT, created_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL, digest TEXT NOT NULL)', schema);
  EXECUTE format('CREATE INDEX IF NOT EXISTS context_traces_created_idx ON %I.context_traces (created_at)', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_trace_put(schema text, trace jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('INSERT INTO %I.context_traces (trace_id, query, route, work_type, created_at, body, digest) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (trace_id) DO NOTHING', schema)
    USING trace->>'traceId', trace->>'query', trace->>'route', trace->>'workType',
          (trace->>'createdAt')::timestamptz, trace, trace->>'digest';
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_trace_get(schema text, trace_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE b jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT body FROM %I.context_traces WHERE trace_id = $1', schema) USING trace_id INTO b;
  RETURN jsonb_build_object('trace', b);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_trace_find(schema text, filters jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
DECLARE lim int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  lim := LEAST(coalesce(NULLIF(filters->>'limit', '')::int, 20), 100);
  EXECUTE format(
    'SELECT coalesce(jsonb_agg(body ORDER BY created_at), ''[]''::jsonb) FROM ('
    'SELECT body, created_at FROM %I.context_traces '
    'WHERE ($1 IS NULL OR route = $1) '
    'AND ($2 IS NULL OR work_type = $2) '
    'AND ($3 IS NULL OR created_at < $3::timestamptz) '
    'AND ($4 IS NULL OR created_at > $4::timestamptz) '
    'AND ($5 IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(body->''candidates'') c WHERE c->>''candidateId'' = $5)) '
    'AND ($6 IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(body->''candidates'') c WHERE c->>''sourceId'' = $6)) '
    'AND ($7 IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(body->''candidates'') c WHERE c->>''terminalStage'' = $7)) '
    'ORDER BY created_at LIMIT $8) t', schema)
    USING filters->>'route', filters->>'workType', filters->>'before', filters->>'after',
          filters->>'chunkId', filters->>'sourceId', filters->>'terminalStage', lim
    INTO rows;
  RETURN jsonb_build_object('traces', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_trace_count(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int; oldest timestamptz; newest timestamptz;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*), min(created_at), max(created_at) FROM %I.context_traces', schema) INTO n, oldest, newest;
  RETURN jsonb_build_object('traces', n, 'oldest', oldest, 'newest', newest);
END;
$$;

-- Open-loop event store -------------------------------------------------------
-- Event wire shape accepts snake_case or camelCase keys (validateLoopEvent);
-- list returns snake_case rows which the client validates into LoopEvent.

CREATE OR REPLACE FUNCTION remembering_loop_initialise(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.open_loop_events (event_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, transition_kind TEXT NOT NULL, from_state TEXT, to_state TEXT, expected JSONB NOT NULL DEFAULT ''{}'', evidence_refs JSONB NOT NULL DEFAULT ''[]'', closure JSONB NOT NULL DEFAULT ''[]'', reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE INDEX IF NOT EXISTS open_loop_events_loop_idx ON %I.open_loop_events (loop_id)', schema);
  EXECUTE format('INSERT INTO %I.meta (key, value) VALUES (''loops.store_version'', ''loops-store-v0.1'') ON CONFLICT (key) DO NOTHING', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_loop_append(schema text, event jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
DECLARE v_event_id text := coalesce(event->>'eventId', event->>'event_id');
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.open_loop_events WHERE event_id = $1', schema) USING v_event_id INTO n;
  IF n > 0 THEN RETURN jsonb_build_object('duplicate', true); END IF;
  EXECUTE format('INSERT INTO %I.open_loop_events (event_id, loop_id, kind, subject, transition_kind, from_state, to_state, expected, evidence_refs, closure, reason, at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)', schema)
    USING v_event_id,
          coalesce(event->>'loopId', event->>'loop_id'),
          event->>'kind',
          event->>'subject',
          coalesce(event->>'transitionKind', event->>'transition_kind'),
          coalesce(event->>'fromState', event->>'from_state'),
          coalesce(event->>'toState', event->>'to_state'),
          coalesce(event->'expected', event->'expected', '{}'::jsonb),
          coalesce(event->'evidenceRefs', event->'evidence_refs', '[]'::jsonb),
          coalesce(event->'closure', '[]'::jsonb),
          coalesce(event->>'reason', ''),
          coalesce((event->>'at'), now()::text)::timestamptz;
  RETURN jsonb_build_object('duplicate', false);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_loop_list(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY at, event_id), ''[]''::jsonb) FROM (SELECT event_id, loop_id, kind, subject, transition_kind, from_state, to_state, expected, evidence_refs, closure, reason, at FROM %I.open_loop_events ORDER BY at, event_id) t', schema) INTO rows;
  RETURN jsonb_build_object('events', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_loop_count(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.open_loop_events', schema) INTO n;
  RETURN jsonb_build_object('count', n);
END;
$$;

-- Explicit-memory write store --------------------------------------------------
-- Record shape (camelCase, mirrors MemoryRecord): recordId, content, role,
-- sourceId, lineageRoot, standingCeiling, state, callerScope, origin,
-- evidenceRefs, eventTime, effectiveFrom, createdAt.
-- Action shape (camelCase, mirrors MemoryActionRecord): actionId, action,
-- recordId, targetRecordId, reason, callerScope, origin, idempotencyKey,
-- relation, createdAt.

CREATE OR REPLACE FUNCTION remembering_write_initialise(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_records (record_id TEXT PRIMARY KEY, content TEXT NOT NULL, role TEXT NOT NULL, source_id TEXT NOT NULL, lineage_root TEXT NOT NULL, standing_ceiling TEXT NOT NULL, state TEXT NOT NULL, caller_scope TEXT NOT NULL, origin TEXT NOT NULL, evidence_refs JSONB NOT NULL DEFAULT ''[]'', event_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_actions (action_id TEXT PRIMARY KEY, action TEXT NOT NULL, record_id TEXT, target_record_id TEXT, reason TEXT NOT NULL DEFAULT '''', caller_scope TEXT NOT NULL, origin TEXT NOT NULL, idempotency_key TEXT, relation JSONB, created_at TIMESTAMPTZ NOT NULL)', schema);
  EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS memory_actions_idem_idx ON %I.memory_actions (idempotency_key) WHERE idempotency_key IS NOT NULL', schema);
  EXECUTE format('INSERT INTO %I.meta (key, value) VALUES (''write.store_version'', ''write-store-v0.1'') ON CONFLICT (key) DO NOTHING', schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_put_record(schema text, record jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('INSERT INTO %I.memory_records (record_id, content, role, source_id, lineage_root, standing_ceiling, state, caller_scope, origin, evidence_refs, event_time, effective_from, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT (record_id) DO NOTHING', schema)
    USING record->>'recordId', record->>'content', record->>'role', record->>'sourceId',
          record->>'lineageRoot', record->>'standingCeiling', record->>'state',
          record->>'callerScope', record->>'origin',
          coalesce(record->'evidenceRefs', '[]'::jsonb),
          (record->>'eventTime')::timestamptz, (record->>'effectiveFrom')::timestamptz,
          (record->>'createdAt')::timestamptz;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_get_record(schema text, record_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE r jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT jsonb_build_object(''recordId'', record_id, ''content'', content, ''role'', role, ''sourceId'', source_id, ''lineageRoot'', lineage_root, ''standingCeiling'', standing_ceiling, ''state'', state, ''callerScope'', caller_scope, ''origin'', origin, ''evidenceRefs'', evidence_refs, ''eventTime'', event_time, ''effectiveFrom'', effective_from, ''createdAt'', created_at) FROM %I.memory_records WHERE record_id = $1', schema) USING record_id INTO r;
  RETURN jsonb_build_object('record', r);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_list_records(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY created_at), ''[]''::jsonb) FROM (SELECT record_id AS "recordId", content, role, source_id AS "sourceId", lineage_root AS "lineageRoot", standing_ceiling AS "standingCeiling", state, caller_scope AS "callerScope", origin, evidence_refs AS "evidenceRefs", event_time AS "eventTime", effective_from AS "effectiveFrom", created_at AS "createdAt" FROM %I.memory_records ORDER BY created_at) t', schema) INTO rows;
  RETURN jsonb_build_object('records', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_update_state(schema text, record_id text, state text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  IF state NOT IN ('active', 'corrected', 'superseded', 'retracted') THEN
    RAISE EXCEPTION 'CONFIG_INVALID: unknown record state %', state;
  END IF;
  EXECUTE format('UPDATE %I.memory_records SET state = $1 WHERE record_id = $2', schema) USING state, record_id;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_put_action(schema text, action jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('INSERT INTO %I.memory_actions (action_id, action, record_id, target_record_id, reason, caller_scope, origin, idempotency_key, relation, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (action_id) DO NOTHING', schema)
    USING action->>'actionId', action->>'action', action->>'recordId', action->>'targetRecordId',
          coalesce(action->>'reason', ''), action->>'callerScope', action->>'origin',
          action->>'idempotencyKey', action->'relation', (action->>'createdAt')::timestamptz;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_get_action(schema text, action_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE r jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT jsonb_build_object(''actionId'', action_id, ''action'', action, ''recordId'', record_id, ''targetRecordId'', target_record_id, ''reason'', reason, ''callerScope'', caller_scope, ''origin'', origin, ''idempotencyKey'', idempotency_key, ''relation'', relation, ''createdAt'', created_at) FROM %I.memory_actions WHERE action_id = $1', schema) USING action_id INTO r;
  RETURN jsonb_build_object('action', r);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_get_idem(schema text, "key" text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE r jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT jsonb_build_object(''actionId'', action_id, ''action'', action, ''recordId'', record_id, ''targetRecordId'', target_record_id, ''reason'', reason, ''callerScope'', caller_scope, ''origin'', origin, ''idempotencyKey'', idempotency_key, ''relation'', relation, ''createdAt'', created_at) FROM %I.memory_actions WHERE idempotency_key = $1', schema) USING "key" INTO r;
  RETURN jsonb_build_object('action', r);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_history(schema text, record_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY created_at), ''[]''::jsonb) FROM (SELECT action_id AS "actionId", action, record_id AS "recordId", target_record_id AS "targetRecordId", reason, caller_scope AS "callerScope", origin, idempotency_key AS "idempotencyKey", relation, created_at AS "createdAt" FROM %I.memory_actions WHERE record_id = $1 OR target_record_id = $1 ORDER BY created_at) t', schema) USING record_id INTO rows;
  RETURN jsonb_build_object('history', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_counts(schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n_records int; n_actions int; by_action jsonb; last_at timestamptz;
BEGIN
  PERFORM remembering_assert_schema(schema);
  EXECUTE format('SELECT count(*) FROM %I.memory_records', schema) INTO n_records;
  EXECUTE format('SELECT count(*), coalesce(jsonb_object_agg(action, cnt), ''{}''::jsonb), max(last_at) FROM (SELECT action, count(*) AS cnt, max(created_at) AS last_at FROM %I.memory_actions GROUP BY 1) g', schema) INTO n_actions, by_action, last_at;
  RETURN jsonb_build_object('records', n_records, 'actions', coalesce(n_actions, 0), 'byAction', coalesce(by_action, '{}'::jsonb), 'lastActionAt', last_at);
END;
$$;
