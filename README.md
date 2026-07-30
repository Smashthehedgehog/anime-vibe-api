# anime-vibe-api

Semantic "vibe" search over the full AniList anime/manga catalog. Given a
natural-language description of a mood or theme, returns the closest-matching
titles by combining sentence-embedding similarity with popularity. Exposed
both as a REST API and as an MCP tool so LLM agents can query it directly.

**Stack:** FastAPI, MCP (SSE transport), Supabase (Postgres + pgvector),
SentenceTransformers (`all-MiniLM-L6-v2`), Render (Blueprint deploy).

## Status

Stages 1-3 complete. Stages 4-5 are still stubs.

## Build stages

1. **DONE** — `supabase/migrations/` (schema: `media_metadata` table +
   `vector` extension) and `ingestion/ingestion_worker.py`, an async worker
   that paginates the AniList GraphQL API for all anime/manga (50/page) and
   upserts metadata into Supabase (embedding left null).
2. **DONE** — `ingestion/vector_worker.py` batch-embeds rows missing a
   vector using `all-MiniLM-L6-v2` and writes them back to Supabase.
   `supabase/migrations/` adds the `match_media` Postgres function (cosine
   similarity + log-normalized popularity) and an HNSW index on
   `embedding` for hybrid search.
3. **DONE** — `app/app.py` (FastAPI service exposing `POST
   /api/v1/search/vibe`), `app/mcp_server.py` (MCP server exposing the
   same search as the `search_anime_manga_vibes` tool, mounted over SSE
   at `/sse` + `/messages/`), `app/search.py` (shared search logic so
   REST and MCP can never disagree), and `app/state.py` (the
   SentenceTransformer + Supabase client, loaded once at startup via
   FastAPI's `lifespan`). See [docs/MCP.md](docs/MCP.md) for how to
   connect an MCP client to this server.
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

### Backfill embeddings

```bash
python ingestion/vector_worker.py
```

Downloads `all-MiniLM-L6-v2` on first run and embeds every row where
`embedding IS NULL` (title + synopsis + genres + tags), 500 rows fetched
at a time. Safe to re-run — it only ever picks up rows still missing a
vector, so partial/interrupted runs resume where they left off.

### Try a hybrid search query

Once some rows are embedded, `match_media` is callable directly from the
Supabase SQL editor or via the client library's `.rpc("match_media", ...)`
with a 384-dim `query_embedding` array, an optional `match_threshold`
(default `0.3`), `match_count` (default `10`), and `media_type`
(`'ANIME'`/`'MANGA'`/omit for both).

### Run the API server

```bash
uvicorn app.app:app --reload
```

Serves:

- `POST /api/v1/search/vibe` — REST search (`{"query": ..., "limit": 10, "type": "ALL"}`)
- `GET /sse` + `POST /messages/` — MCP server (SSE transport), tool
  `search_anime_manga_vibes`. See [docs/MCP.md](docs/MCP.md) for how to
  connect a client and why it's shaped this way.
- `GET /healthz` — readiness check (`{"status": "ok"}` once the
  embedding model and Supabase client have finished loading)
- `GET /docs` — interactive OpenAPI docs for the REST surface

CORS is locked down to the origins listed in `CORS_ALLOWED_ORIGINS`
(comma-separated) — set that in `.env` before testing from a browser.

**No auth yet** — stage 4 adds API-key + rate-limit enforcement in front
of both the REST and MCP routes. Don't deploy this publicly before then.

## Environment variables

See [.env.example](.env.example).
