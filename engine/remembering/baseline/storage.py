# Ported from ernanhughes/project-memory (Apache-2.0) into the opencode-remembering bundled engine. Behavior preserved.
# 
# 
"""PostgreSQL substrate: sources, chunks, full-text search, pgvector.

One store holds everything: source records, chunk text, ordinary
metadata, the FTS index, and the vector index. The schema is versioned
in ``meta``; embedding identity (provider, model, dimension) is stored
per chunk so stale vectors are detectable, never silent.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql

SCHEMA_VERSION = "0.1.0"


@dataclass(frozen=True)
class ScoredChunk:
    chunk_id: str
    source_id: str
    text: str
    section: str | None
    score: float
    rank: int


class Store:
    def __init__(self, dsn: str, schema: str = "baseline") -> None:
        self.dsn = dsn
        self.schema = schema
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._embedding_dim: int | None = None

    def initialise(self, embedding_dim: int) -> None:
        """Create schema and tables. Safe to rerun; refuses dim changes."""
        self._embedding_dim = embedding_dim
        s = sql.Identifier(self.schema)
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s))
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                ).format(s)
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.sources (
                        source_id TEXT PRIMARY KEY,
                        artifact_type TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        timestamp TEXT,
                        ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                ).format(s)
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.chunks (
                        chunk_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL REFERENCES {}.sources(source_id)
                            ON DELETE CASCADE,
                        ordinal INT NOT NULL,
                        text TEXT NOT NULL,
                        section TEXT,
                        char_start INT NOT NULL,
                        char_end INT NOT NULL,
                        content_hash TEXT NOT NULL,
                        chunker TEXT NOT NULL,
                        embedding_version TEXT NOT NULL,
                        embedding vector({}) NOT NULL,
                        tsv TSVECTOR NOT NULL
                    )
                    """
                ).format(s, s, sql.Literal(str(embedding_dim)))
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON {}.chunks "
                    "USING GIN (tsv)"
                ).format(s)
            )
            # NOTE: every statement in this module is schema-qualified, so
            # the search_path is deliberately left untouched. Narrowing
            # it would hide the extension schemas (public) and break
            # opclass lookups such as vector_cosine_ops.
        self._check_dimension(embedding_dim)

    def _check_dimension(self, embedding_dim: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT atttypmod FROM pg_attribute "
                    "JOIN pg_class ON pg_class.oid = pg_attribute.attrelid "
                    "JOIN pg_namespace ON pg_namespace.oid = "
                    "pg_class.relnamespace "
                    "WHERE pg_namespace.nspname = %s "
                    "AND pg_class.relname = 'chunks' "
                    "AND pg_attribute.attname = 'embedding'"
                ),
                (self.schema,),
            )
            row = cur.fetchone()
            # pgvector records the dimension directly as the typmod
            # (no varchar-style length offset).
            if row is not None and row[0] != -1:
                actual = row[0]
                if actual != embedding_dim:
                    raise ValueError(
                        f"embedding dimension mismatch: table holds {actual}, "
                        f"configuration requests {embedding_dim}. Re-embed "
                        "with a fresh schema instead of mixing dimensions."
                    )

    # -- sources ------------------------------------------------------

    def upsert_source(
        self,
        source_id: str,
        artifact_type: str,
        content_hash: str,
        timestamp: str | None,
    ) -> bool:
        """Insert or refresh a source record. Returns True when changed."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT content_hash FROM {}.sources WHERE source_id = %s"
                ).format(sql.Identifier(self.schema)),
                (source_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] == content_hash:
                return False
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.sources
                        (source_id, artifact_type, content_hash, timestamp)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_id) DO UPDATE SET
                        artifact_type = EXCLUDED.artifact_type,
                        content_hash = EXCLUDED.content_hash,
                        timestamp = EXCLUDED.timestamp,
                        ingested_at = now()
                    """
                ).format(sql.Identifier(self.schema)),
                (source_id, artifact_type, content_hash, timestamp),
            )
            return True

    def known_sources(self) -> dict[str, str]:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT source_id, content_hash FROM {}.sources").format(
                    sql.Identifier(self.schema)
                )
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def artifact_types(self, source_ids: list[str]) -> dict[str, str]:
        """Artifact type per source (for evidence classification)."""
        if not source_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT source_id, artifact_type FROM {}.sources "
                    "WHERE source_id = ANY(%s)"
                ).format(sql.Identifier(self.schema)),
                (source_ids,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def remove_source(self, source_id: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {}.sources WHERE source_id = %s").format(
                    sql.Identifier(self.schema)
                ),
                (source_id,),
            )
            return cur.rowcount

    def replace_chunks(
        self,
        source_id: str,
        rows: list[tuple],
    ) -> None:
        """Replace all chunks of one source (delete + insert)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {}.chunks WHERE source_id = %s").format(
                    sql.Identifier(self.schema)
                ),
                (source_id,),
            )
            if rows:
                cur.executemany(
                    sql.SQL(
                        """
                        INSERT INTO {}.chunks
                            (chunk_id, source_id, ordinal, text, section,
                             char_start, char_end, content_hash, chunker,
                             embedding_version, embedding, tsv)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                to_tsvector('english', %s))
                        """
                    ).format(sql.Identifier(self.schema)),
                    [r + (r[3],) for r in rows],
                )

    # -- retrieval ----------------------------------------------------

    def lexical_search(self, query: str, k: int) -> list[ScoredChunk]:
        """Full-text search with OR semantics over query terms.

        Plain AND matching (plainto_tsquery) retrieves nothing for
        ordinary question phrasing, where no single passage shares every
        query term. OR-of-terms with ts_rank_cd ordering is the fairer
        conventional baseline: partial overlap ranks, full overlap wins.
        """
        import re as _re

        terms = [
            t for t in _re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1
        ]
        if not terms:
            return []
        or_query = " | ".join(terms)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    SELECT chunk_id, source_id, text, section,
                           ts_rank_cd(tsv, to_tsquery('english', %s))
                    FROM {}.chunks
                    WHERE tsv @@ to_tsquery('english', %s)
                    ORDER BY 5 DESC, chunk_id
                    LIMIT %s
                    """
                ).format(sql.Identifier(self.schema)),
                (or_query, or_query, k),
            )
            return [
                ScoredChunk(row[0], row[1], row[2], row[3], float(row[4]), i + 1)
                for i, row in enumerate(cur.fetchall())
            ]

    def dense_search(
        self, vector: list[float], k: int, hnsw_probe: int = 16
    ) -> list[ScoredChunk]:
        literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
        with self.conn.cursor() as cur:
            # GUC assignment takes no bind parameters; probe is int-cast.
            cur.execute(f"SET LOCAL hnsw.ef_search = {int(hnsw_probe)}")
            cur.execute(
                sql.SQL(
                    """
                    SELECT chunk_id, source_id, text, section,
                           1 - (embedding <=> %s::vector)
                    FROM {}.chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """
                ).format(sql.Identifier(self.schema)),
                (literal, literal, k),
            )
            return [
                ScoredChunk(row[0], row[1], row[2], row[3], float(row[4]), i + 1)
                for i, row in enumerate(cur.fetchall())
            ]

    def chunks_for_sources(self, source_ids: list[str]) -> list[ScoredChunk]:
        if not source_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    SELECT chunk_id, source_id, text, section, 1.0
                    FROM {}.chunks
                    WHERE source_id = ANY(%s)
                    ORDER BY source_id, ordinal
                    """
                ).format(sql.Identifier(self.schema)),
                (source_ids,),
            )
            return [
                ScoredChunk(row[0], row[1], row[2], row[3], 1.0, i + 1)
                for i, row in enumerate(cur.fetchall())
            ]

    def ensure_hnsw(self) -> bool:
        """Build the HNSW index when the vector width allows it.

        pgvector caps HNSW-indexable width (2000 dimensions in the
        verified 0.8.6 build). Wider embeddings still work through
        exact search; the index is then absent and health reports it
        as a scale limitation rather than a defect. Returns True when
        the index exists afterwards.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw "
                        "ON {}.chunks USING hnsw "
                        "(embedding vector_cosine_ops)"
                    ).format(sql.Identifier(self.schema))
                )
            return True
        except psycopg.errors.ProgramLimitExceeded:
            return False

    # -- health --------------------------------------------------------

    def stats(self) -> dict:
        out: dict = {}
        with self.conn.cursor() as cur:
            for label, query in [
                ("sources", "SELECT count(*) FROM {}.sources"),
                ("chunks", "SELECT count(*) FROM {}.chunks"),
                (
                    "duplicate_chunks",
                    "SELECT count(*) - count(DISTINCT content_hash) "
                    "FROM {}.chunks",
                ),
                (
                    "missing_vectors",
                    "SELECT count(*) FROM {}.chunks WHERE embedding IS NULL",
                ),
                (
                    "distinct_embedding_versions",
                    "SELECT count(DISTINCT embedding_version) FROM {}.chunks",
                ),
                (
                    "distinct_chunkers",
                    "SELECT count(DISTINCT chunker) FROM {}.chunks",
                ),
            ]:
                cur.execute(sql.SQL(query).format(sql.Identifier(self.schema)))
                out[label] = cur.fetchone()[0]
            cur.execute(
                sql.SQL(
                    "SELECT embedding_version, count(*) FROM {}.chunks "
                    "GROUP BY 1 ORDER BY 2 DESC"
                ).format(sql.Identifier(self.schema))
            )
            out["embedding_versions"] = [
                {"version": r[0], "chunks": r[1]} for r in cur.fetchall()
            ]
            cur.execute(
                sql.SQL(
                    "SELECT to_regclass('{}.chunks_tsv_idx'), "
                    "to_regclass('{}.chunks_embedding_hnsw')"
                ).format(
                    sql.Identifier(self.schema), sql.Identifier(self.schema)
                )
            )
            row = cur.fetchone()
            out["fts_index"] = row[0] if row else None
            out["hnsw_index"] = row[1] if len(row) > 1 else None
        return out

    def orphan_chunks(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.chunks c LEFT JOIN {}.sources s "
                    "ON c.source_id = s.source_id WHERE s.source_id IS NULL"
                ).format(
                    sql.Identifier(self.schema), sql.Identifier(self.schema)
                )
            )
            return cur.fetchone()[0]

    def close(self) -> None:
        self.conn.close()
