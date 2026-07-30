# Method 2: the AI recommendation agent

There are two ways to get an anime/manga recommendation from this API:

| | Method 1: search | Method 2: recommend |
|---|---|---|
| Endpoint | `POST /api/v1/search/vibe` | `POST /api/v1/recommend` |
| Returns | Ranked list (up to `limit`) | One pick, with reasoning |
| How it decides | Cosine similarity + popularity, deterministic | An LLM (Groq) calls the search tool itself and judges the results |
| Uses an LLM? | No | Yes |

This doc covers method 2. See the main [README](../README.md) and
[docs/MCP.md](MCP.md) for method 1 and the MCP server it's built on.

## What's actually happening

`app/recommend_agent.py` connects to **this server's own `/sse` MCP
endpoint as a real client** — the exact same protocol an external agent
like Claude Desktop would use to reach `search_anime_manga_vibes` (see
[docs/MCP.md](MCP.md)). It is not a shortcut that calls the search logic
directly in-process. Concretely, on each `POST /api/v1/recommend`:

1. Open an SSE connection to `/sse`, authenticated with **the caller's
   own `X-API-Key`** — there's no separate internal/bypass credential,
   so the search calls this triggers count against the same rate limit
   as everything else that key does.
2. Ask the MCP server what tools it has (`list_tools`) and hand that
   schema straight to Groq — MCP tool schemas are already JSON Schema,
   so no translation step is needed to use them as Groq/OpenAI-style
   function definitions.
3. Give Groq's `llama-3.3-70b-versatile` the user's vibe and let it
   decide whether to call `search_anime_manga_vibes` (it can call it
   more than once — e.g. retry with a narrower query if the first batch
   doesn't fit well — up to 3 rounds).
4. Each tool call the model requests is executed for real, through the
   MCP `call_tool` protocol, and the JSON result is fed back to the
   model.
5. Once the model stops requesting tool calls, its final message is the
   answer. The system prompt requires it to end with a
   `RECOMMENDATION_ID: <id>` line naming one of the titles the tool
   actually returned; the server parses that out and attaches the full
   record (cover image, genres, etc.) to the response rather than
   asking the frontend to parse it out of prose.

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
  "explanation": "I recommend Apocalypse Hotel because...",
  "media": { "id": 180675, "type": "ANIME", "title_english": "Apocalypse Hotel", "...": "..." },
  "tool_calls": [
    { "tool": "search_anime_manga_vibes", "arguments": { "query": "hopeful post-apocalyptic story about rebuilding", "media_type": "ANIME", "limit": 10 } }
  ]
}
```

`media` is `null` if the agent never called the tool, or if its final
`RECOMMENDATION_ID` didn't match anything it actually saw — treated as a
failed recommendation rather than trusted blindly, since the system
prompt explicitly forbids inventing a title but nothing stops a model
from ignoring instructions.

`tool_calls` is the full trace of what the agent searched for — useful
for debugging why it picked what it picked, and shown in the test
frontend for exactly that reason.

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
