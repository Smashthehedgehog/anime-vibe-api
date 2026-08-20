-- Trims media_metadata down to the top 2,500 most popular ANIME and the
-- top 2,500 most popular MANGA (5,000 rows total), from ~123,720. Two
-- separate curated pools, not one pooled top-5000 -- each type keeps its
-- own ranking so MANGA titles don't get crowded out by ANIME's larger,
-- more popular catalog.
--
-- Why: the full catalog made HNSW indexing too compute-intensive to
-- build on Supabase's free tier (see the now-superseded
-- rebuild_hnsw_index_m8 migration's comment), so Direct Search ran
-- unindexed. A table this size makes that a non-issue -- see the
-- migration immediately after this one, which rebuilds the index at
-- pgvector's standard m=16 now that storage is no longer the binding
-- constraint it was at 123k rows.
--
-- A full data-only backup of every row (including embeddings) was taken
-- before this ran (via `supabase db dump --linked --data-only`) -- ask
-- if you need the full catalog restored or want to re-expand later.
--
-- Also: the app layer (schemas.py, mcp_server.py, search.py) no longer
-- accepts a combined/"ALL" media_type -- callers must specify ANIME or
-- MANGA. This migration doesn't need to do anything for that (match_media
-- already required an explicit type or NULL for "both"), it's called out
-- here just so the two changes are understood as one deliberate pivot.

with ranked as (
    select
        id,
        row_number() over (
            partition by type
            order by popularity desc, id asc
        ) as rank_in_type
    from media_metadata
)
delete from media_metadata
where id in (
    select id from ranked where rank_in_type > 2500
);
