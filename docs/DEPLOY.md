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
4. Deploy. First build takes a couple minutes (installing dependencies
   and downloading the embedding model — see below); later deploys reuse
   Docker layer caching for anything that didn't change.
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
7. **HNSW index note**: as of 2026-08-20 (see "Why a curated 5,000-title
   catalog" below), `media_metadata` is trimmed to 5,000 rows
   specifically so this index builds cleanly — it no longer hits the
   Cloudflare-proxied Management API timeout that blocked it at 123k
   rows. If it's ever dropped again (manually, or a fresh Supabase
   project that hasn't run the migrations in `supabase/migrations/`
   yet), search still works correctly, just without the fast-path index
   (a few seconds per query instead of milliseconds, since it falls
   back to a full scan) — not a blocker for deploying, just worth
   knowing if search feels slow.

## Why fastembed, not sentence-transformers/torch

This ran on `sentence-transformers` (torch) through 2026-08-21. Real
production evidence pointed at it as the cause of repeated OOM crashes
on Render's free 512 MB instance -- including one that happened purely
during startup, before a single request was served, confirmed via
Render's own "Ran out of memory (used over 512MB) while running your
code" event. Measured directly, in the actual Linux base image this
project builds on (`python:3.11-slim`), not guessed at:

| | `sentence-transformers` (torch) | `fastembed` (onnxruntime) |
|---|---|---|
| torch itself | 1.2 GB | -- (not installed) |
| NVIDIA/CUDA libraries | 2.7 GB (**entirely unused** -- Render's free tier has no GPU) | -- (not installed) |
| Full image size | ~4+ GB of just these deps | 740 MB total image |
| Weight-loading time in a live boot log | 33-80s (this was most of every cold start) | sub-second once baked into the image |

The catch with swapping embedding libraries is usually that a
different model produces a different, incompatible vector space --
but `fastembed` runs the *exact same* `all-MiniLM-L6-v2` weights
through ONNX Runtime instead of torch, so this isn't a different
model, just a lighter runtime for the same one. Verified directly
before switching: encoding the same text with both libraries produced
vectors with cosine similarity 1.0000000014 (max per-element
difference ~1e-7, pure floating-point noise) -- so every embedding
already stored in `media_metadata` from the old library stayed valid;
no re-embedding was needed.

Also verified end-to-end, not just at the library level: built the
real image, ran it with `docker run --memory=512m` (matching Render's
free-tier limit exactly), and exercised both `/api/v1/search/vibe` and
`/api/v1/recommend` against it -- memory stayed flat around ~210 MB
the entire time, including through a full multi-round `/recommend`
call (the exact request shape that used to OOM).

## Why the model is pre-downloaded *and* forced offline

`build.sh` loads a `fastembed.TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')`
once during `docker build`, which downloads the ONNX weights into
`$HF_HOME` (passed explicitly as `cache_dir` -- fastembed doesn't read
`$HF_HOME` on its own) and bakes them into the image layer. The point
is to avoid a network round-trip to Hugging Face on every cold start.

That alone isn't enough, though: `huggingface_hub` (a `fastembed`
dependency, same as it was for `sentence-transformers`) still makes a
handful of live `HEAD` requests to `huggingface.co` on *every* model
load -- even when the weights are already cached -- to revalidate that
the cache is current. Left as-is, that reintroduces the exact network
dependency the build-time download was meant to remove. The Dockerfile
sets `HF_HUB_OFFLINE=1` for the runtime container (deliberately *not*
during the build step, which still needs network to fetch the weights
the first time) to skip that revalidation entirely.

This was verified directly, not assumed: the image was built and run
locally with `docker run --network none` (networking fully disabled at
the container level) and the model still loaded and encoded correctly
-- proof startup has no live dependency on Hugging Face being
reachable.

## Why a curated 5,000-title catalog

`media_metadata` was trimmed on 2026-08-20 from the full AniList
catalog (~123,720 titles) down to the top 2,500 most popular ANIME and
the top 2,500 most popular MANGA — two separate pools, 5,000 rows
total (see `supabase/migrations/20260820233038_trim_to_top_2500_per_type.sql`).
This was a real, largely irreversible production change; a full
data-only backup of every row (including embeddings) was taken first
via `supabase db dump --linked --data-only`, so the full catalog can
be restored or re-expanded later if wanted.

**Why:** the full catalog made HNSW indexing too compute-intensive to
build on Supabase's free tier — Direct Search ran unindexed as a
result (a few seconds per query instead of milliseconds). At 5,000
rows, HNSW builds trivially; see
`supabase/migrations/20260820233100_rebuild_hnsw_index_m16.sql`, which
also reverts the index's `m` parameter from 8 back to pgvector's
default 16 (m=8 was chosen earlier specifically to fit the *default*
index under the free tier's storage cap at 123k rows — storage is no
longer the binding constraint at 5,000).

**Side effect:** the API no longer accepts a combined/"ALL" `type` —
callers must specify `"ANIME"` or `"MANGA"` (see `app/schemas.py`).
Mixing two curated, unrelated top-2500 pools into one ranked list
would've been a worse experience than picking one and searching it
properly.

## Keeping the catalog fresh

There's no automatic refresh (see the free-plan tradeoff at the top of
this doc) — `media_metadata` only updates when you run the workers
yourself. **Important, given the curation above:** `ingestion_worker.py`
re-upserts the *entire* AniList catalog, not just the curated 5,000 —
running it alone would silently balloon `media_metadata` back toward
123k rows and undo the trim. Always follow it with the same trim SQL:

```bash
python ingestion/ingestion_worker.py   # re-upserts everything (cheap for
                                        # already-seen ids, it's an upsert)
python ingestion/vector_worker.py      # embeds whatever came in with a
                                        # null embedding -- only genuinely
                                        # new/changed titles get re-embedded
```

```sql
-- Re-run against production (Supabase SQL editor, or `psql`/the CLI) --
-- same logic as 20260820233038_trim_to_top_2500_per_type.sql, safe to
-- run repeatedly. Re-ranks by *current* popularity and re-trims to the
-- top 2,500 per type, whatever ingestion just added or changed.
with ranked as (
    select id, row_number() over (
        partition by type order by popularity desc, id asc
    ) as rank_in_type
    from media_metadata
)
delete from media_metadata where id in (select id from ranked where rank_in_type > 2500);
```

If you want this automated again later without paying for a Render cron
job, the `anime-vibe-refresh` service definition (git history, this
commit's parent) can be restored — or point a free external scheduler
(e.g. a GitHub Actions workflow on a `schedule:` trigger, running in
GitHub's own compute, not Render's) at these same two scripts plus the
trim query above.

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

## Why `/api/v1/recommend` doesn't open a second network connection to itself

The recommendation agent (`app/recommend_agent.py`) is a real MCP client
against this server's own MCP server object — same `ClientSession`, same
tool schema, same tool implementation an external client like Claude
Desktop would use. It originally did that over a live SSE connection to
this same process's own `/sse` route, to exercise the full wire
protocol. On Render's free instance (512 MB RAM), that meant one process
holding the loaded embedding model, a second live HTTP/SSE socket to
itself, and several rounds of Groq calls with full candidate payloads,
all at once — confirmed (via Render's logs and by reproducing it
directly against the live deployment) to reliably OOM-kill the
container on every `/api/v1/recommend` call. It now connects over
`mcp.shared.memory`'s in-process transport instead — the real MCP
protocol and tool implementation, just without the redundant loopback
socket, which was the actually expensive part. See
[docs/RECOMMEND.md](RECOMMEND.md) for the full writeup.
