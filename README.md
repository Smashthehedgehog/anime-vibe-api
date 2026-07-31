# anime-vibe-api

Semantic "vibe" search over the full AniList anime/manga catalog. Given a
natural-language description of a mood or theme, returns the closest-matching
titles by combining sentence-embedding similarity with popularity. Exposed
both as a REST API and as an MCP tool so LLM agents can query it directly.

**Stack:** FastAPI, MCP (SSE transport), Supabase (Postgres + pgvector),
SentenceTransformers (`all-MiniLM-L6-v2`), Render (Blueprint deploy).

## Status

All 5 stages complete.

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
4. **DONE** — `app/auth.py`: API key validation (SHA-256 hashed in
   Supabase's `api_keys` table) and per-key rate limiting via
   `api_usage_logs`, enforced on both the REST route (a FastAPI
   `Depends()`) and the MCP `/sse` connection (an ASGI-level guard, since
   the mounted MCP app has no FastAPI route to attach a dependency to).
   `POST /api/v1/keys/generate` issues new keys, gated by
   `ADMIN_MASTER_TOKEN`. See [docs/AUTH.md](docs/AUTH.md).
5. **DONE** — `Dockerfile` + `build.sh` pre-download `all-MiniLM-L6-v2`
   weights at build time and set `HF_HUB_OFFLINE=1` for the runtime
   container, so startup makes zero network calls to Hugging Face (see
   [docs/DEPLOY.md](docs/DEPLOY.md) for why that matters and how it was
   verified). `render.yaml` defines the Blueprint: the always-on web
   service plus a cron job that re-runs `ingestion_worker.py` then
   `vector_worker.py` every Sunday at 02:00 UTC, reusing the same image.

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

- `POST /api/v1/search/vibe` — REST search, method 1 (`{"query": ..., "limit": 10, "type": "ALL"}`), requires `X-API-Key`
- `POST /api/v1/recommend` — AI recommendation, method 2
  (`{"vibe": ..., "type": "ALL"}`), requires `X-API-Key`. A Groq LLM
  acting as a real MCP client against this server's own `/sse` endpoint
  gathers candidates via the search tool, then ranks its own top 10 with
  a reason each — its judgment, not the raw similarity ranking.
  See [docs/RECOMMEND.md](docs/RECOMMEND.md).
- `GET /sse` + `POST /messages/` — MCP server (SSE transport), tool
  `search_anime_manga_vibes`, requires `X-API-Key`. See
  [docs/MCP.md](docs/MCP.md) for how to connect a client.
- `POST /api/v1/keys/generate` — issue a new API key, requires
  `X-Admin-Token: $ADMIN_MASTER_TOKEN`. See [docs/AUTH.md](docs/AUTH.md).
- `GET /healthz` — readiness check (`{"status": "ok"}` once the
  embedding model and Supabase client have finished loading), no auth
- `GET /docs` — interactive OpenAPI docs for the REST surface, no auth

CORS is locked down to the origins listed in `CORS_ALLOWED_ORIGINS`
(comma-separated) — set that in `.env` before testing from a browser.

### Test frontend

A minimal, dependency-free HTML/JS client for manually exercising both
methods lives in [frontend/index.html](frontend/index.html) — a toggle
switches between direct search and the AI recommendation agent. It's a
plain static file — no build step. Serve it as real HTTP (not `file://`,
which CORS handles inconsistently across browsers):

```bash
cd frontend && python -m http.server 3000
```

Add `http://localhost:3000` to `CORS_ALLOWED_ORIGINS` and restart the API
server (CORS origins are read once at startup), then open
`http://localhost:3000`. Paste in an API key (see below), type a vibe,
and hit Search.

### Issuing an API key

```bash
export ADMIN_MASTER_TOKEN=...   # same value the running server has

curl -X POST http://127.0.0.1:8000/api/v1/keys/generate \
  -H "X-Admin-Token: $ADMIN_MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "me", "rate_limit_per_hour": 60}'
```

The `api_key` in the response is shown once — save it. Full details
(revoking keys, how the rate limit is computed, why a shared admin token
instead of full user auth) are in [docs/AUTH.md](docs/AUTH.md).

## Deploying

```bash
docker build -t anime-vibe-api .
```

builds the production image locally (installs deps, bakes in the
embedding model weights). Deploying is via a Render Blueprint
(`render.yaml`) — connect this repo in the Render dashboard and it
provisions the web service + weekly refresh cron job together. See
[docs/DEPLOY.md](docs/DEPLOY.md) for the full walkthrough, including why
the container is forced fully offline (`HF_HUB_OFFLINE=1`) at runtime.

## Environment variables

See [.env.example](.env.example).
