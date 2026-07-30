# anime-vibe-api

Semantic "vibe" search over the full AniList anime/manga catalog. Given a
natural-language description of a mood or theme, returns the closest-matching
titles by combining sentence-embedding similarity with popularity. Exposed
both as a REST API and as an MCP tool so LLM agents can query it directly.

**Stack:** FastAPI, MCP (SSE transport), Supabase (Postgres + pgvector),
SentenceTransformers (`all-MiniLM-L6-v2`), Render (Blueprint deploy).

## Status

Stage 1 complete. Stages 2-5 are still stubs.

## Build stages

1. **DONE** — `supabase/migrations/` (schema: `media_metadata` table +
   `vector` extension) and `ingestion/ingestion_worker.py`, an async worker
   that paginates the AniList GraphQL API for all anime/manga (50/page) and
   upserts metadata into Supabase (embedding left null).
2. **`ingestion/vector_worker.py` + `sql/`** — batch-embeds rows missing a
   vector using `all-MiniLM-L6-v2`, writes them back to Supabase, and adds
   the `match_media` Postgres function (cosine similarity + popularity,
   HNSW index) for hybrid search.
3. **`app/app.py`, `app/mcp_server.py`, `app/schemas.py`** — FastAPI service
   exposing `POST /api/v1/search/vibe` (REST) and an MCP server
   (`search_anime_manga_vibes` tool) mounted over SSE on the same instance.
   Model loaded once at startup via FastAPI `lifespan`.
4. **`app/auth.py`** — API key issuance/validation (SHA-256 hashed in
   Supabase) and per-key rate limiting via a usage-log table, applied to
   both the REST routes and the MCP SSE connection.
5. **`Dockerfile`, `render.yaml`, `build.sh`** — containerizes the service
   (pre-downloading model weights at build time) and defines a Render
   Blueprint: the web service plus a weekly cron job that re-runs the
   ingestion and vectorization workers.

## Local setup

```bash
cp .env.example .env   # fill in Supabase credentials
pip install -r requirements.txt
```

### Apply the schema (Supabase CLI)

This repo uses `supabase/migrations/` rather than pasting SQL into the
dashboard. From the repo root:

```bash
npx supabase login                      # one-time browser auth
npx supabase link --project-ref <ref>   # find <ref> in your project's URL/settings
npx supabase db push                    # applies supabase/migrations/*.sql
```

(`link`/`push` need your own Supabase login, so this step is on you — an
agent shouldn't hold your account credentials.)

### Run the ingestion worker

```bash
python ingestion/ingestion_worker.py
```

Paginates all AniList anime + manga and upserts them into `media_metadata`.
Expect this to take a while — AniList has tens of thousands of entries and
the worker throttles itself against their rate limit.

## Environment variables

See [.env.example](.env.example).
