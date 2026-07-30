"""Stage 1: paginate the AniList GraphQL API for all anime/manga and
upsert metadata into Supabase `media_metadata` (embedding left null).

TODO:
- async pagination loop, 50 per page, both ANIME and MANGA
- respect X-RateLimit-Remaining
- upsert via supabase-py client
- structured logging
"""
