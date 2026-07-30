-- vector_worker.py needs to write only the `embedding` column for a batch
-- of rows without touching anything else. supabase-py's .upsert() does a
-- real INSERT ... ON CONFLICT DO UPDATE against the *entire* row shape --
-- any column missing from the payload (title_english, synopsis, genres,
-- tags, popularity, ...) gets overwritten with its default/null, not left
-- alone. Confirmed directly against media_metadata before this migration
-- existed: upserting just {id, embedding} attempted to null out every
-- other column and only failed loudly because `type` is NOT NULL --
-- columns without such a constraint would have been silently wiped.
--
-- This function does a genuine targeted UPDATE instead, touching only
-- `embedding`, so it's safe to call repeatedly with partial data.
create or replace function bulk_update_embeddings(updates jsonb)
returns void
language plpgsql
as $$
begin
    update media_metadata m
    set embedding = (u->>'embedding')::vector
    from jsonb_array_elements(updates) as u
    where m.id = (u->>'id')::integer;
end;
$$;

comment on function bulk_update_embeddings is
    'Batched embedding-only update. updates is a JSON array of '
    '{"id": int, "embedding": [384 floats]} objects. Used by '
    'vector_worker.py instead of .upsert() to avoid clobbering other '
    'columns with defaults.';
