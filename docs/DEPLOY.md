# Deploying to Render

This repo deploys as a [Render Blueprint](https://render.com/docs/blueprint-spec)
(`render.yaml`), which provisions two services from one Docker image
(`Dockerfile`):

| Service | Type | What it does |
|---|---|---|
| `anime-vibe-api` | `web` | Runs `uvicorn app.app:app` — the REST + MCP server. Always on. |
| `anime-vibe-refresh` | `cron` | Overrides the image's CMD (via `dockerCommand`) to run `ingestion_worker.py` then `vector_worker.py`, Sundays at 02:00 UTC. |

## Steps

1. Push this repo to GitHub (already done).
2. In the Render dashboard: **New +** → **Blueprint** → select this repo.
   Render reads `render.yaml` and shows both services before creating
   anything.
3. Render will prompt for every env var marked `sync: false` in
   `render.yaml` — currently `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `ADMIN_MASTER_TOKEN` (web only), and `CORS_ALLOWED_ORIGINS` (web
   only). These are never written to `render.yaml` itself, so nothing
   sensitive is in git.
4. Deploy. First build takes several minutes (installing `torch` +
   `sentence-transformers` and downloading the embedding model — see
   below); later deploys reuse Docker layer caching for anything that
   didn't change.
5. Once the web service is live, confirm `GET /<service-url>/healthz`
   returns `{"status": "ok"}`, then issue yourself an API key (see
   [docs/AUTH.md](AUTH.md)) and try `POST /api/v1/search/vibe`.
6. The `media_metadata` table starts empty — the cron job populates it
   on its first scheduled run, or trigger it manually from the Render
   dashboard ("Run Job") instead of waiting until Sunday.

## Why the model is pre-downloaded *and* forced offline

`build.sh` imports `SentenceTransformer('all-MiniLM-L6-v2')` once during
`docker build`, which downloads the weights into `$HF_HOME` and bakes
them into the image layer. The point is to avoid a network round-trip to
Hugging Face on every cold start.

That alone isn't enough, though: `huggingface_hub` (a `sentence-transformers`
dependency) still makes a handful of live `HEAD` requests to
`huggingface.co` on *every* model load — even when the weights are
already cached — to revalidate that the cache is current. Left as-is,
that reintroduces the exact network dependency the build-time download
was meant to remove, and adds real latency (measured: ~1.5-2s of HEAD
requests during `docker run` before this fix). The Dockerfile sets
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` for the runtime
container (deliberately *not* during the build step, which still needs
network to fetch the weights the first time) to skip that
revalidation entirely.

This was verified directly, not assumed: the image was built and run
locally with `docker run --network none` (networking fully disabled at
the container level) and the server still started and served
`/healthz` in about a second — proof startup has no live dependency on
Hugging Face being reachable.

## Keeping the catalog fresh

The cron job exists so `media_metadata` doesn't go stale as AniList adds
new releases: `ingestion_worker.py` re-upserts everything (cheap for
already-seen IDs, since it's an upsert), then `vector_worker.py` embeds
whatever came in with a null `embedding` — i.e. only genuinely new or
changed titles get re-embedded, not the whole catalog every week.

## Local equivalent of the Render build

To reproduce exactly what Render does before pushing (useful when
editing the Dockerfile):

```bash
docker build -t anime-vibe-api .
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e SUPABASE_URL=... \
  -e SUPABASE_SERVICE_ROLE_KEY=... \
  -e CORS_ALLOWED_ORIGINS=https://your-portfolio-domain.com \
  anime-vibe-api
```
