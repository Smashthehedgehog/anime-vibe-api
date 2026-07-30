"""Stage 2: embed media_metadata rows where embedding IS NULL using
all-MiniLM-L6-v2 (title + synopsis + genres + tags), batched, then write
the vectors back to Supabase.

TODO: fetch missing rows, batch-encode, update rows.
"""
