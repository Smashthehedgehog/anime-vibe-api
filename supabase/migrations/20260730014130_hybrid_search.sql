-- Stage 2: hybrid (semantic + popularity) search.

-- HNSW index for fast approximate cosine-distance search over embeddings.
create index if not exists media_metadata_embedding_hnsw_idx
    on media_metadata
    using hnsw (embedding vector_cosine_ops);

-- Combines cosine similarity with a log-normalized popularity score so
-- well-known titles are favored among otherwise-similar matches.
create or replace function match_media(
    query_embedding vector(384),
    match_threshold float default 0.3,
    match_count int default 10,
    media_type text default null
)
returns table (
    id integer,
    type text,
    title_english text,
    synopsis text,
    genres text[],
    tags text[],
    popularity integer,
    cover_image_url text,
    similarity float,
    score float
)
language sql
stable
as $$
    with bounds as (
        select greatest(max(popularity), 1) as max_popularity
        from media_metadata
    ),
    candidates as (
        select
            m.*,
            1 - (m.embedding <=> query_embedding) as similarity
        from media_metadata m
        where m.embedding is not null
            and (media_type is null or m.type = media_type)
    )
    select
        c.id,
        c.type,
        c.title_english,
        c.synopsis,
        c.genres,
        c.tags,
        c.popularity,
        c.cover_image_url,
        c.similarity,
        (0.85 * c.similarity)
            + (0.15 * (ln(c.popularity + 1) / ln(b.max_popularity + 1))) as score
    from candidates c, bounds b
    where c.similarity > match_threshold
    order by score desc
    limit match_count;
$$;

comment on function match_media is
    'Hybrid search: 85% cosine similarity to query_embedding + 15% log-normalized popularity.';
