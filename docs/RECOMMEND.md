# Method 2: the AI recommendation agent

There are two ways to get an anime/manga recommendation from this API:

| | Method 1: search | Method 2: recommend |
|---|---|---|
| Endpoint | `POST /api/v1/search/vibe` | `POST /api/v1/recommend` |
| Returns | Ranked list (up to `limit`) | Its own top 10, each with a reason |
| How it decides | Cosine similarity + popularity, deterministic | An LLM (Groq) calls the search tool itself, then ranks what it found by its own judgment |
| Uses an LLM? | No | Yes |

This doc covers method 2. See the main [README](../README.md) and
[docs/MCP.md](MCP.md) for method 1 and the MCP server it's built on.

## What's actually happening

`app/recommend_agent.py` connects to **this server's own `/sse` MCP
endpoint as a real client** — the exact same protocol an external agent
like Claude Desktop would use to reach `search_anime_manga_vibes` (see
[docs/MCP.md](MCP.md)). It is not a shortcut that calls the search logic
directly in-process. Two phases, on each `POST /api/v1/recommend`:

**Phase 1 — gather.** Open an SSE connection to `/sse`, authenticated
with **the caller's own `X-API-Key`** (no separate internal/bypass
credential — these search calls count against the same rate limit as
everything else that key does). Ask the MCP server what tools it has
(`list_tools`) and hand that schema straight to Groq — MCP tool schemas
are already JSON Schema, so no translation is needed. Give Groq's
`llama-3.3-70b-versatile` the user's vibe and let it call
`search_anime_manga_vibes` as many times as it wants (up to 3 rounds,
requesting ~20-30 results per call so there's a real pool to choose
from) — e.g. a second, narrower search if the first batch feels thin.
Every candidate it sees across all calls is accumulated. If the model
never calls the tool at all, one direct fallback search runs so phase 2
always has real data to work with.

**Phase 2 — rank.** A separate request (Groq's JSON mode, no tools this
time) asks the model to rank the top 10 best fits from *only* the
candidates it actually saw in phase 1, each with a one-sentence reason.
Every returned id is cross-checked against the real candidate pool —
anything that doesn't match (a hallucinated id, or one repeated) is
dropped rather than trusted. The result is the ordering shown to the
user, capped at 10.

Why go through the MCP protocol at all, given the agent and the tool
live in the same process: this endpoint exists specifically to exercise
the MCP surface with a genuine tool-calling LLM. Calling the search
function directly would be simpler and marginally faster, but would
defeat the point of building this particular feature.

## Why Groq

Fast (their inference hardware is built for low latency) and effectively
free for a project at this scale, with an OpenAI-compatible
tools/function-calling API — `tool.inputSchema` from MCP's `list_tools()`
passes straight through as a Groq function's `parameters`.

## Request / response

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recommend \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"vibe": "a hopeful post-apocalyptic story about rebuilding", "type": "ANIME"}'
```

```json
{
  "vibe": "a hopeful post-apocalyptic story about rebuilding",
  "recommendations": [
    {
      "rank": 1,
      "reason": "Apocalypse Hotel focuses on rebuilding and quiet perseverance rather than survival horror...",
      "media": { "id": 180675, "type": "ANIME", "title_english": "Apocalypse Hotel", "similarity": 0.39, "score": 0.45, "...": "..." }
    },
    { "rank": 2, "reason": "...", "media": { "...": "..." } }
  ],
  "tool_calls": [
    { "tool": "search_anime_manga_vibes", "arguments": { "query": "hopeful post-apocalyptic story about rebuilding", "media_type": "ANIME", "limit": 25 } }
  ]
}
```

`recommendations` is ordered by the model's own judgment, **not** by
each item's `similarity`/`score` fields — those are still present on
every `media` record (they're just the shared search-result shape), but
phase 2 ranks on the model's reasoning, not those numbers. The test
frontend deliberately doesn't render `similarity`/`score` for this
method, to keep the distinction visible: this list is AI judgment, not
raw cosine-similarity ranking.

`recommendations` is `[]` if the agent (and the phase-1 fallback) never
turned up any real candidates. `tool_calls` is the trace of what got
searched for during phase 1 — returned in the API response for
debugging, but intentionally not rendered in the frontend.

## Environment variables

```
GROQ_API_KEY=...                              # console.groq.com, free tier
GROQ_MODEL=llama-3.3-70b-versatile
INTERNAL_MCP_URL=http://127.0.0.1:8000/sse    # where the agent reaches its own MCP server
```

`INTERNAL_MCP_URL` matters most for deployment: it defaults to
`127.0.0.1`, which is only correct when the agent and the MCP server are
literally the same running process on the same host (true for the
`uvicorn app.app:app` setup this project uses). If that ever changes,
this needs to point wherever the service can actually reach its own
`/sse` endpoint from.
