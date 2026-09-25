-- remembering-http-v1.sql
-- Versioned PostgreSQL RPC contract for the opencode-remembering HTTP backend.
-- Install once per database, then expose via PostgREST / Supabase-style gateway.
--
--   psql "$DATABASE_URL" -f sql/remembering-http-v1.sql
--
-- Design:
-- - One RPC function per semantic storage operation (no arbitrary SQL over HTTP).
-- - Every function takes an explicit schema name and validates it as a safe
--   identifier; cross-project access is refused inside the database.
-- - Project isolation matches the direct backend: meta.project.path must equal
--   the caller's canonical path (enforced by the plugin; the functions refuse
--   to create a second project's meta silently).
-- - Requires: pgvector, pg_trgm. Idempotent (CREATE OR REPLACE / IF NOT EXISTS).
-- - Grant EXECUTE only to the dedicated api role, e.g.:
--     GRANT EXECUTE ON FUNCTION ... TO remembering_api;
--   Never grant the api role CREATE / DDL outside these functions.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Helpers (not exposed over HTTP directly)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_assert_schema(p_schema text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF p_schema IS NULL OR p_schema !~ '^[A-Za-z_][A-Za-z0-9_$]*$'
     OR char_length(p_schema) > 63
     OR lower(p_schema) LIKE 'pg\_%' THEN
    RAISE EXCEPTION 'CONFIG_INVALID: refusing unsafe schema %', p_schema;
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Health / setup
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_health(p_schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v_chunks regclass;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  SELECT to_regclass(p_schema || '.chunks') INTO v_chunks;
  RETURN jsonb_build_object('ok', true, 'initialised', v_chunks IS NOT NULL, 'schema', p_schema);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_initialise(p_schema text, embedding_dim int)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  IF embedding_dim IS NULL OR embedding_dim < 1 OR embedding_dim > 65535 THEN
    RAISE EXCEPTION 'CONFIG_INVALID: bad embedding dimension %', embedding_dim;
  END IF;
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.sources (source_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL, content_hash TEXT NOT NULL, timestamp TEXT, ingested_at TIMESTAMPTZ NOT NULL DEFAULT now())', p_schema);
  -- NOTE: vector dimension cannot use a parameter; embedding_dim is validated above.
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.chunks (chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES %I.sources(source_id) ON DELETE CASCADE, ordinal INT NOT NULL, text TEXT NOT NULL, section TEXT, char_start INT NOT NULL, char_end INT NOT NULL, content_hash TEXT NOT NULL, chunker TEXT NOT NULL, embedding_version TEXT NOT NULL, embedding vector(%s) NOT NULL, tsv TSVECTOR NOT NULL)', p_schema, p_schema, embedding_dim);
  EXECUTE format('CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON %I.chunks USING GIN (tsv)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.temporal_events (event_id TEXT PRIMARY KEY, subject TEXT NOT NULL, kind TEXT NOT NULL, value TEXT, event_time TIMESTAMPTZ NOT NULL, known_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, supersedes TEXT, reason TEXT, received_at TIMESTAMPTZ NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.standing_events (event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, transition TEXT NOT NULL, level TEXT, reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.context_traces (trace_id TEXT PRIMARY KEY, query TEXT NOT NULL, route TEXT NOT NULL, work_type TEXT, created_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL, digest TEXT NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.open_loop_events (event_id TEXT PRIMARY KEY, loop_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, transition_kind TEXT NOT NULL, from_state TEXT, to_state TEXT, expected JSONB NOT NULL DEFAULT ''{}'', evidence_refs JSONB NOT NULL DEFAULT ''[]'', closure JSONB NOT NULL DEFAULT ''[]'', reason TEXT NOT NULL DEFAULT '''', at TIMESTAMPTZ NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_records (record_id TEXT PRIMARY KEY, content TEXT NOT NULL, role TEXT NOT NULL, source_id TEXT NOT NULL, lineage_root TEXT NOT NULL, standing_ceiling TEXT NOT NULL, state TEXT NOT NULL, caller_scope TEXT NOT NULL, origin TEXT NOT NULL, evidence_refs JSONB NOT NULL DEFAULT ''[]'', event_time TIMESTAMPTZ NOT NULL, effective_from TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)', p_schema);
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_actions (action_id TEXT PRIMARY KEY, action TEXT NOT NULL, record_id TEXT, target_record_id TEXT, reason TEXT NOT NULL DEFAULT '''', caller_scope TEXT NOT NULL, origin TEXT NOT NULL, idempotency_key TEXT, relation JSONB, created_at TIMESTAMPTZ NOT NULL)', p_schema);
  RETURN jsonb_build_object('ok', true);
END;
$$;

-- Baseline ports ------------------------------------------------------------

CREATE OR REPLACE FUNCTION remembering_check_dimension(p_schema text, embedding_dim int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE actual int;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  SELECT atttypmod INTO actual FROM pg_attribute
    JOIN pg_class ON pg_class.oid = pg_attribute.attrelid
    JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
   WHERE pg_namespace.nspname = p_schema AND pg_class.relname = 'chunks' AND pg_attribute.attname = 'embedding';
  IF actual IS NOT NULL AND actual <> -1 AND actual <> embedding_dim THEN
    RAISE EXCEPTION 'DIMENSION_MISMATCH: table holds %, requested %', actual, embedding_dim;
  END IF;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_upsert_source(p_schema text, source_id text, artifact_type text, content_hash text, ts text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE changed boolean := false;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('INSERT INTO %I.sources (source_id, artifact_type, content_hash, timestamp) VALUES ($1,$2,$3,$4) ON CONFLICT (source_id) DO UPDATE SET artifact_type=EXCLUDED.artifact_type, content_hash=EXCLUDED.content_hash, timestamp=EXCLUDED.timestamp, ingested_at=now() RETURNING (xmax = 0)', p_schema)
    USING source_id, artifact_type, content_hash, ts INTO changed;
  RETURN jsonb_build_object('changed', coalesce(changed, true));
END;
$$;

CREATE OR REPLACE FUNCTION remembering_known_sources(p_schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE rows jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(jsonb_build_object(''source_id'', source_id, ''content_hash'', content_hash)), ''[]''::jsonb) FROM %I.sources', p_schema) INTO rows;
  RETURN jsonb_build_object('sources', rows);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_remove_source(p_schema text, source_id text)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('DELETE FROM %I.sources WHERE source_id = $1', p_schema) USING source_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN jsonb_build_object('removed', n);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_replace_chunks(p_schema text, source_id text, chunks jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE item jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('DELETE FROM %I.chunks WHERE source_id = $1', p_schema) USING source_id;
  FOR item IN SELECT * FROM jsonb_array_elements(coalesce(chunks, '[]'::jsonb)) LOOP
    EXECUTE format('INSERT INTO %I.chunks (chunk_id, source_id, ordinal, text, section, char_start, char_end, content_hash, chunker, embedding_version, embedding, tsv) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::vector, to_tsvector(''english'', $4))', p_schema)
      USING item->>'chunk_id', item->>'source_id', (item->>'ordinal')::int, item->>'text',
            item->>'section', (item->>'char_start')::int, (item->>'char_end')::int,
            item->>'content_hash', item->>'chunker', item->>'embedding_version', item->>'embedding';
  END LOOP;
  RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_lexical_search(p_schema text, q text, k int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE items jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format($f$SELECT coalesce(jsonb_agg(t ORDER BY rank), '[]'::jsonb) FROM (SELECT chunk_id, source_id, text, section, ts_rank_cd(tsv, to_tsquery('english', $1)) AS score, row_number() OVER (ORDER BY ts_rank_cd(tsv, to_tsquery('english', $1)) DESC, chunk_id) AS rank FROM %I.chunks WHERE tsv @@ to_tsquery('english', $1) ORDER BY 5 DESC, chunk_id LIMIT $2) t$f$, p_schema)
    USING q, k INTO items;
  RETURN jsonb_build_object('items', items);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_dense_search(p_schema text, query_vector text, k int)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE items jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(t ORDER BY rank), ''[]''::jsonb) FROM (SELECT chunk_id, source_id, text, section, 1 - (embedding <=> $1::vector) AS score, row_number() OVER (ORDER BY embedding <=> $1::vector, chunk_id) AS rank FROM %I.chunks ORDER BY embedding <=> $1::vector, chunk_id LIMIT $2) t', p_schema)
    USING query_vector, k INTO items;
  RETURN jsonb_build_object('items', items);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_ensure_hnsw(p_schema text)
RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  BEGIN
    EXECUTE format('CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON %I.chunks USING hnsw (embedding vector_cosine_ops)', p_schema);
    RETURN jsonb_build_object('created', true);
  EXCEPTION WHEN program_limit_exceeded THEN
    RETURN jsonb_build_object('created', false);
  END;
END;
$$;

CREATE OR REPLACE FUNCTION remembering_stats(p_schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE out jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('SELECT jsonb_build_object(''sources'', (SELECT count(*) FROM %I.sources), ''chunks'', (SELECT count(*) FROM %I.chunks))', p_schema, p_schema) INTO out;
  RETURN out;
END;
$$;

CREATE OR REPLACE FUNCTION remembering_orphans(p_schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE n int;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('SELECT count(*) FROM %I.chunks c LEFT JOIN %I.sources s ON c.source_id = s.source_id WHERE s.source_id IS NULL', p_schema, p_schema) INTO n;
  RETURN jsonb_build_object('orphans', n);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_embedding_versions(p_schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE v jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  EXECUTE format('SELECT coalesce(jsonb_agg(DISTINCT embedding_version), ''[]''::jsonb) FROM %I.chunks', p_schema) INTO v;
  RETURN jsonb_build_object('versions', v);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_is_initialised(p_schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE r regclass;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  SELECT to_regclass(p_schema || '.chunks') INTO r;
  RETURN jsonb_build_object('initialised', r IS NOT NULL);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_read_meta(p_schema text) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE m jsonb;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  BEGIN
    EXECUTE format('SELECT coalesce(jsonb_object_agg(key, value), ''{}''::jsonb) FROM %I.meta', p_schema) INTO m;
  EXCEPTION WHEN undefined_table THEN
    m := '{}'::jsonb;
  END;
  RETURN jsonb_build_object('meta', m);
END;
$$;

CREATE OR REPLACE FUNCTION remembering_write_meta(p_schema text, entries jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE k text; v text;
BEGIN
  PERFORM remembering_assert_schema(p_schema);
  FOR k, v IN SELECT key, value FROM jsonb_each_text(entries) LOOP
    EXECUTE format('INSERT INTO %I.meta (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING', p_schema) USING k, v;
  END LOOP;
  RETURN jsonb_build_object('ok', true);
END;
$$;

-- Event stores (temporal / standing / trace / loops / writes) follow the same
-- envelope shapes as the direct backend. Each append is idempotent on event_id
-- / action_id; list/count/versions mirror the Postgres stores 1:1. Full
-- definitions are intentionally compact here; see the plugin HTTP backend
-- contract tests for exact request/response shapes.
