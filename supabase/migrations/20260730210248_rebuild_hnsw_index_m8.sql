-- Rebuild the HNSW index with a smaller `m` (max graph connections per
-- node) than pgvector's default of 16. Storage scales roughly with m,
-- and this project is storage-constrained on Supabase's free tier
-- (the default-m index was ~193MB, out of a 500MB cap) -- m=8 roughly
-- halves that cost. The recall trade-off (occasionally missing the
-- exact best match in favor of a very close one) is invisible for a
-- vibe-search recommender, where there's no single "correct" answer.
--
-- Explicitly drops first rather than relying on `if not exists`: the
-- earlier hybrid_search migration already creates an index with this
-- same name at the default m. On a fresh environment applying every
-- migration in order, `create index if not exists` would see that
-- index already exists and do nothing, silently keeping m=16. Dropping
-- first makes the end state m=8 regardless of whether this runs after
-- a fresh migration history or (as on this live project) after the
-- index was already manually dropped for space.
drop index if exists media_metadata_embedding_hnsw_idx;

create index media_metadata_embedding_hnsw_idx
    on media_metadata
    using hnsw (embedding vector_cosine_ops)
    with (m = 8);
