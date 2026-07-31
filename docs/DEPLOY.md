# Deploying to Render

This repo deploys as a [Render Blueprint](https://render.com/docs/blueprint-spec)
(`render.yaml`), which provisions one service from the Docker image
(`Dockerfile`):

| Service | Type | What it does |
|---|---|---|
| `anime-vibe-api` | `web`, **free plan** | Runs `uvicorn app.app:app` — the REST + MCP server. |

**Why no cron job / why free plan, specifically:** Render's Cron Job
service type cannot run on the free tier at all — confirmed directly
against Render's own docs: *"Other service types don't support Free
instances."* Even on a paid plan, a cron job has a real minimum cost
(Render: *"a minimum monthly charge of $1 per cron job service"*).
Rather than pay for a weekly auto-refresh, that job was dropped — see
"Keeping the catalog fresh" below for the manual alternative. The
tradeoff of staying on the free web service plan: it *"spin[s] down
after 15 minutes without receiving any inbound traffic,"* so the first
request after any idle gap eats a real cold-start (Render waking the
container, on top of this app's own startup time — which is fast once
awake, see below). If that tradeoff isn't worth it, switch `plan: free`
to `plan: starter` in `render.yaml` for an always-on service.

## Steps

1. Push this repo to GitHub (already done).
2. In the Render dashboard: **New +** → **Blueprint** → select this repo.
   Render reads `render.yaml` and shows the service before creating
   anything.
3. Render will prompt for every env var marked `sync: false` in
   `render.yaml` — currently `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `ADMIN_MASTER_TOKEN`, `CORS_ALLOWED_ORIGINS`, and `GROQ_API_KEY`.
   These are never written to `render.yaml` itself, so nothing sensitive
   is in git. `GROQ_API_KEY` powers the `/api/v1/recommend` agent (see
   [docs/RECOMMEND.md](RECOMMEND.md)) — get one free at
   [console.groq.com](https://console.groq.com).
4. Deploy. First build takes several minutes (installing `torch` +
   `sentence-transformers` and downloading the embedding model — see
   below); later deploys reuse Docker layer caching for anything that
   didn't change.
5. Once the web service is live, confirm `GET /<service-url>/healthz`
   returns `{"status": "ok"}`, then issue an API key for whatever's
   going to call this (a portfolio frontend, a script, yourself) via
   `POST /api/v1/keys/generate` (see [docs/AUTH.md](AUTH.md)) and try
   `POST /api/v1/search/vibe`.
6. If you're pointing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` at a
   project that's already been through stages 1-2 (ingestion +
   embedding), `media_metadata` is already populated — nothing further
   to do. For a genuinely fresh Supabase project, the table starts
   empty — see "Keeping the catalog fresh" below to populate it.
7. **HNSW index note**: if the project's HNSW index (see
   [docs/RECOMMEND.md](RECOMMEND.md) / the migrations in
   `supabase/migrations/`) was ever dropped and not rebuilt — e.g.
   because building it hit Supabase's Cloudflare-proxied Management API
   timeout on a large, already-populated table — search still works
   correctly, just without the fast-path index (a few seconds per query
   instead of milliseconds, since it falls back to a full scan). Not a
   blocker for deploying, just worth knowing if search feels slow.

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

There's no automatic refresh (see the free-plan tradeoff at the top of
this doc) — `media_metadata` only updates when you run the workers
yourself. Whenever you want to pick up new/changed AniList titles,
run both against the **production** Supabase project (point `.env` at
its `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`, not local dev's):

```bash
python ingestion/ingestion_worker.py   # re-upserts everything (cheap for
                                        # already-seen ids, it's an upsert)
python ingestion/vector_worker.py      # embeds whatever came in with a
                                        # null embedding -- only genuinely
                                        # new/changed titles get re-embedded
```

If you want this automated again later without paying for a Render cron
job, the `anime-vibe-refresh` service definition (git history, this
commit's parent) can be restored — or point a free external scheduler
(e.g. a GitHub Actions workflow on a `schedule:` trigger, running in
GitHub's own compute, not Render's) at these same two scripts.

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
  -e GROQ_API_KEY=... \
  anime-vibe-api
```

`INTERNAL_MCP_URL` doesn't need to be set here — it derives itself from
`$PORT` (see `app/recommend_agent.py`), matching whatever port this
container is actually bound to.
