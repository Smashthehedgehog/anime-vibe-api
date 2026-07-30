-- Stage 1: core schema for the semantic vibe recommender.

create extension if not exists vector;

create table if not exists media_metadata (
    id integer primary key,
    type text not null check (type in ('ANIME', 'MANGA')),
    title_english text,
    synopsis text,
    genres text[] not null default '{}',
    tags text[] not null default '{}',
    popularity integer not null default 0,
    cover_image_url text,
    embedding vector(384),
    updated_at timestamptz not null default now()
);

comment on table media_metadata is
    'AniList anime/manga metadata with a 384-dim SentenceTransformer embedding for semantic search.';

comment on column media_metadata.embedding is
    'all-MiniLM-L6-v2 embedding of title + synopsis + genres + tags. Null until the vector_worker backfills it.';

create index if not exists media_metadata_type_idx on media_metadata (type);
