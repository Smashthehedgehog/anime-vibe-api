-- vector_worker.py repeatedly queries `WHERE embedding IS NULL LIMIT 500`
-- to find its next batch. Early in a backfill that's cheap (most rows
-- match), but as the null rows become a shrinking fraction of the table,
-- a sequential scan has to examine more and more non-matching rows to
-- collect 500 matches -- and eventually times out. Confirmed directly:
-- with ~6,800 of 123,720 rows left null, both this query and a plain
-- COUNT(*) ... WHERE embedding IS NULL started hitting Postgres's
-- statement_timeout (57014).
--
-- A partial index only covering the null rows keeps this query at
-- index-scan speed regardless of how sparse the remaining nulls get. It
-- shrinks automatically as rows get embedded and is effectively free
-- once the backfill finishes (indexing zero rows).
create index if not exists media_metadata_pending_embedding_idx
    on media_metadata (id)
    where embedding is null;
