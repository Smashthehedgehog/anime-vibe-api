# Connecting to the MCP server

This API exposes its search as an [MCP](https://modelcontextprotocol.io)
tool, `search_anime_manga_vibes`, in addition to the plain REST endpoint.
This doc is for connecting an MCP-aware client (Claude, another agent, a
test script) to it. If you just want to call the API from a webpage, use
`POST /api/v1/search/vibe` instead — see the main [README](../README.md).

## What "MCP server" means here, concretely

MCP standardizes how an LLM agent discovers and calls tools it didn't
know about ahead of time. Instead of hardcoding "POST this JSON to this
URL," an MCP client connects to the server, asks "what tools do you
have?", and gets back a name, a description, and a typed argument schema
for each one — in this case, one tool:

| Field | Value |
|---|---|
| Tool name | `search_anime_manga_vibes` |
| Arguments | `query` (string, required), `media_type` (`"ALL"` \| `"ANIME"` \| `"MANGA"`, default `"ALL"`), `limit` (int 1-50, default 10) |
| Returns | `{ "query": ..., "count": ..., "results": [...] }` — same shape as the REST endpoint's response body |

The tool's docstring (in `app/mcp_server.py`) is what the client's LLM
actually reads to decide when this tool is relevant and how to fill in
its arguments — it's written as a prompt, not as internal documentation.

## Transport: SSE

This server implements MCP's **SSE transport**, which is two HTTP routes
working together (implemented for us by the `mcp` SDK's `FastMCP`, see
`app/mcp_server.py` and `app/app.py`):

- `GET /sse` — the client opens this and keeps it open; the server
  streams JSON-RPC responses back over it.
- `POST /messages/` — the client posts each outgoing JSON-RPC request
  here (initialize, list tools, call a tool). The HTTP response to the
  POST itself is just an acknowledgement; the real result arrives
  asynchronously on the open `/sse` stream.

You generally don't hand-construct these requests — an MCP client
library manages both sides of that handshake for you. The URL you give
the client is the `/sse` endpoint.

Local dev: `http://127.0.0.1:8000/sse`
Deployed (stage 5): `https://<your-render-service>.onrender.com/sse`

> **Auth required.** As of stage 4, every connection to `/sse` must carry
> a valid `X-API-Key` header — `app/auth.py`'s `ApiKeyASGIGuard` checks it
> before the SSE stream even opens, so a missing or invalid key gets a
> `401` instead of a working connection. Get a key from someone with
> `ADMIN_MASTER_TOKEN` (see [README](../README.md#issuing-an-api-key)) —
> there's no self-serve signup yet.

## Connecting a client

### Option A — a generic MCP client / agent framework

Most MCP client configs want a name and a URL, with the transport set to
SSE (sometimes labeled "remote"/"HTTP" server, as opposed to a local
`command`-based stdio server). Wherever your client's config lives:

```json
{
  "mcpServers": {
    "anime-vibe-search": {
      "transport": "sse",
      "url": "http://127.0.0.1:8000/sse",
      "headers": {
        "X-API-Key": "<your key>"
      }
    }
  }
}
```

Consult your specific client's docs for the exact key names — some
clients call this a "remote server" or "connector" rather than exposing
raw transport/url fields, and not all of them support setting custom
headers on an SSE connection. If yours doesn't, use Option B, or a
reverse proxy that injects the header. The important part is: transport
is SSE, the URL is this server's `/sse` path (not `/messages/`, which
the client manages internally), and the header carries your key.

### Option B — the official Python `mcp` SDK (for testing / scripting)

```python
import asyncio

from mcp import ClientSession
from mcp.client.sse import sse_client

API_KEY = "<your key>"  # see README: issuing an API key


async def main():
    async with sse_client(
        "http://127.0.0.1:8000/sse", headers={"X-API-Key": API_KEY}
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print([t.name for t in tools.tools])

            result = await session.call_tool(
                "search_anime_manga_vibes",
                {
                    "query": "a hopeful post-apocalyptic story about rebuilding",
                    "media_type": "ANIME",
                    "limit": 5,
                },
            )
            print(result)


asyncio.run(main())
```

This is the fastest way to sanity-check the server end-to-end without
setting up a full agent client.

## Running the server locally to test against

```bash
cp .env.example .env   # SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
pip install -r requirements.txt
uvicorn app.app:app --reload
```

Then either point Option B's script at `http://127.0.0.1:8000/sse`, or
check `GET http://127.0.0.1:8000/healthz` first to confirm the model and
Supabase client finished loading (`{"status": "ok"}`).

## Why SSE and not stdio or Streamable HTTP

- **stdio** (spawning the server as a local subprocess and talking over
  stdin/stdout) only makes sense for a server running on the same
  machine as the client — not applicable once this is deployed on
  Render and called by remote agents.
- **Streamable HTTP** (a single POST endpoint) is the newer MCP
  transport and would also work here; SSE was chosen because it's the
  more broadly supported transport across current MCP clients at the
  time this was built. Revisit this if client support shifts.
