-- Rebuilds the HNSW index at pgvector's standard m=16, now that
-- media_metadata is trimmed to 5,000 rows (see the trim migration
-- immediately before this one). The earlier rebuild_hnsw_index_m8
-- migration dropped m to 8 specifically because the *default* m=16
-- index was ~193MB against a 500MB free-tier cap at 123,720 rows --
-- storage was the binding constraint, not compute. At 5,000 rows that
-- constraint is gone (the index is a small fraction of that size
-- regardless of m), so this reverts to m=16 for the better recall,
-- rather than leaving m=8 as unnecessary residue from a problem this
-- table no longer has.
drop index if exists media_metadata_embedding_hnsw_idx;

create index media_metadata_embedding_hnsw_idx
    on media_metadata
    using hnsw (embedding vector_cosine_ops)
    with (m = 16);
