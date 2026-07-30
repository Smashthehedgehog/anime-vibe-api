# anime-vibe-api

Semantic "vibe" search over the full AniList anime/manga catalog. Given a
natural-language description of a mood or theme, returns the closest-matching
titles by combining sentence-embedding similarity with popularity. Exposed
both as a REST API and as an MCP tool so LLM agents can query it directly.

**Stack:** FastAPI, MCP (SSE transport), Supabase (Postgres + pgvector),
SentenceTransformers (`all-MiniLM-L6-v2`), Render (Blueprint deploy).

## Status

Scaffolding only — directory structure and stubs are in place; each stage
below still needs to be implemented.

## Build stages

1. **`sql/` + `ingestion/ingestion_worker.py`** — Supabase schema
   (`media_metadata` table, `vector` extension) and an async worker that
   paginates the AniList GraphQL API for all anime/manga and upserts
   metadata (embedding left null).
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

Run the SQL in `sql/` against your Supabase project (SQL Editor) before
running either worker.

## Environment variables

See [.env.example](.env.example).
