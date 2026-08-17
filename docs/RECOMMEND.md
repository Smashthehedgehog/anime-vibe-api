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

`app/recommend_agent.py` acts as **a real MCP client against this
server's own MCP server object** (`mcp_server.mcp`) — the same
`ClientSession`, the same tool schema from `list_tools()`, the same
`search_anime_manga_vibes` tool implementation an external agent like
Claude Desktop would reach over `/sse` (see [docs/MCP.md](MCP.md)). It
is not a shortcut that calls the search logic directly in-process — it
goes through the actual MCP protocol, just over an in-process transport
rather than a second live network connection (see "Why in-process, not
a second SSE connection" below). Two phases, on each
`POST /api/v1/recommend`:

**Phase 1 — gather.** Open a `ClientSession` against our own MCP server
and ask it what tools it has (`list_tools`), handing that schema
straight to Groq — MCP tool schemas are already JSON Schema, so no
translation is needed. Give Groq's `openai/gpt-oss-120b` the user's
vibe and let it call `search_anime_manga_vibes` as many times as it
wants (up to `MAX_TOOL_ROUNDS` rounds, requesting ~10-15 results per
call so there's a real pool to choose from without ballooning memory
on a 512 MB instance — see "Why in-process, not a second SSE
connection" below) — e.g. a second, narrower search if the first batch
feels thin. Every candidate it sees across all calls is accumulated,
up to `MAX_CANDIDATES` total. If the model never calls the tool at
all, one direct fallback search runs so phase 2 always has real data
to work with.

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
function directly (bypassing MCP entirely) would be simpler and
marginally faster, but would defeat the point of building this
particular feature.

## Why in-process, not a second SSE connection

This originally worked by opening a real SSE connection from the agent
to this same process's own `/sse` route — the full network round-trip,
to demonstrate the wire protocol end to end, not just the client
library. That turned out not to survive contact with Render's free
instance (512 MB RAM): one process simultaneously holding the loaded
embedding model, a second live HTTP/SSE socket talking to itself, and
several rounds of Groq calls each carrying full candidate payloads
(title, synopsis, genres, tags for ~20-30 items) reliably OOM-killed the
container on every `/api/v1/recommend` call — confirmed both in Render's
own logs and by reproducing the crash directly against the live
deployment (a request that returns Render's edge 502 page in well under
a second, rather than our own app's 502, is this happening: the process
died before it could even respond).

The fix keeps the actual MCP protocol intact — real `ClientSession`,
real `Server`, real JSON-RPC messages, the real tool schema and
implementation — but swaps the transport for
`mcp.shared.memory.create_connected_server_and_client_session`, which
wires the client and server together over in-process memory streams
instead of a socket. The redundant network/HTTP/SSE layer was the
expensive part, not the protocol itself; removing it took the endpoint
from a reliable crash to a reliable success. Candidate payloads fed back
into the LLM's own message history are also trimmed (full synopsis
truncated to `SYNOPSIS_CHARS_FOR_LLM`, and fields the model doesn't need
to reason about ranking — like `cover_image_url`, `similarity`, `score`
— dropped) so that history doesn't grow unbounded across rounds; the
full, untrimmed record is still what's returned to the caller as
`media`.

One behavior change from this: because the internal tool calls no
longer go through the ASGI-level `X-API-Key` guard in front of `/sse`,
they're no longer separately metered against the caller's rate limit —
only the outer `POST /api/v1/recommend` call itself is (via
`require_api_key`). Previously, a single recommend call with 3 gather
rounds could consume up to 4 units of a key's hourly quota (1 for the
recommend call, up to 3 more for the internal searches it triggered);
now it consumes exactly 1, which is arguably the more correct behavior
for what is, from the caller's perspective, a single logical request.

**Update, one week later:** the above fix stopped this from failing on
*every* call, but Render's event log still showed a real OOM
(`Ran out of memory (used over 512MB) while running your code`) under
real traffic on 2026-08-17. The embedding model + torch already
consume a large, fixed share of that 512 MB baseline (shared with
Direct Search too, which has never shown this problem) — no amount of
trimming on the recommend side changes that baseline, it only shrinks
the *marginal* growth this endpoint adds on top of it. Tightened
further: `MAX_TOOL_ROUNDS` 3 → 2, the per-call result count clamped to
20 at the call site (not just suggested at 10-15 in the prompt), and a
hard `MAX_CANDIDATES` (40) ceiling on how many unique candidates
`seen_candidates` will ever hold in one request, regardless of how
many rounds/calls happen. This reduces the worst case; it does not
guarantee it can't happen again on the free tier. Render's Standard
plan (2 GB RAM, $25/month) is the only way to make this a non-issue
outright — a real cost/reliability tradeoff, left as an open decision
rather than made unilaterally.

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
GROQ_MODEL=openai/gpt-oss-120b
```

No URL/host config needed — the agent connects to the `mcp` server
object directly in-process (see "Why in-process, not a second SSE
connection" above), so there's nothing that varies by deployment target.

**On the model name specifically:** Groq periodically retires older
models. This project originally ran on `llama-3.3-70b-versatile`; Groq
removed it at some point without any advance signal visible from this
side, and every `/api/v1/recommend` call started failing with a 404
(`model_not_found`) from Groq's own API — confirmed directly by
calling `POST https://api.groq.com/openai/v1/chat/completions` with
that model name and getting back `"The model ... does not exist or
you do not have access to it."`. `GROQ_MODEL` exists as an env var
specifically so this doesn't require a code change to fix — if
recommend calls start failing, check
[console.groq.com/docs/models](https://console.groq.com/docs/models)
for the current list before assuming it's a code bug. Any currently
active model with `"tools"` in its `supported_features` (check
`GET /openai/v1/models`) works as a drop-in replacement.
