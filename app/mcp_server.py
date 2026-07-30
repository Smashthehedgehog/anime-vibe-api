"""MCP (Model Context Protocol) server: exposes vibe search as a *tool*
that any MCP-aware LLM client (Claude, or a custom agent built on the
`mcp` Python/TypeScript SDK) can call directly, without knowing this is
a REST API underneath. See docs/MCP.md for how to point a real client
(Claude, a Python script, curl) at this server once it's running.

----------------------------------------------------------------------
Why this exists alongside the REST endpoint (app.py)
----------------------------------------------------------------------
REST (`POST /api/v1/search/vibe`) is for a caller -- typically a browser
frontend -- that already knows exactly what request shape it wants and
just needs an HTTP response.

MCP is for an *agent*: given a natural-language goal, it has to discover
"is there a tool that can help with this?" and work out on its own how
to call it and what to do with the result. MCP standardizes that
discovery-and-invocation handshake so any MCP client can talk to any MCP
server with zero bespoke integration code. Concretely, this file does
two things:

  1. Declares one tool, `search_anime_manga_vibes`, with a typed
     signature and a docstring. That docstring is not internal dev
     documentation -- the MCP SDK ships it to the client as the tool's
     description, and the *calling LLM* reads it to decide whether this
     tool is relevant to the user's request and how to fill in its
     arguments. Write it the way you'd write a prompt, not a comment.
  2. Delegates to the exact same `vibe_search()` helper the REST
     endpoint uses (see search.py), so an agent calling this tool and a
     browser calling the REST endpoint with an equivalent query get
     identical results.

----------------------------------------------------------------------
Transport: SSE (Server-Sent Events)
----------------------------------------------------------------------
MCP defines a JSON-RPC 2.0 message format for capability discovery and
tool calls, independent of how those messages actually travel over the
wire. Several transports exist; this server uses SSE, one of the two
options that work over plain HTTP (the other, newer one is "Streamable
HTTP" -- a single POST-based endpoint. stdio, the third option, only
works for a local subprocess, which doesn't apply to a server deployed
on Render). Concretely, SSE transport is two HTTP endpoints working
together:

  * GET /sse       The client opens this and keeps it open. The server
                    uses it to push JSON-RPC responses and events back
                    to the client as they become available.
  * POST /messages/ The client POSTs each outgoing JSON-RPC message here
                    (e.g. "list your tools", "call tool X with these
                    arguments"). The HTTP response to *this* POST is
                    just a 202-style acknowledgement -- the actual
                    result is delivered asynchronously on the open
                    /sse stream, correlated by JSON-RPC request ID.

That split -- POST to send, long-lived GET to receive -- is what lets an
MCP server live behind a normal HTTPS load balancer while still being
able to push data whenever it's ready, rather than the client having to
poll.

FastMCP (`mcp.server.fastmcp.FastMCP`, part of the official Python `mcp`
SDK) implements all of the above -- the two routes, the JSON-RPC
envelope, request/response correlation -- for us. The only MCP-specific
code we have to write is the `@mcp.tool()` function below; `mcp.sse_app()`
(called from app.py) hands back a ready-to-mount Starlette app with
/sse and /messages/ wired up.

----------------------------------------------------------------------
Auth note
----------------------------------------------------------------------
As of this stage there is no authentication on /sse or /messages/ --
anyone with the URL can call the tool. Stage 4 adds an X-API-Key check
in front of both this and the REST route. Don't point real traffic at
this deployment until that lands.
"""

from __future__ import annotations

import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP

from app.search import vibe_search
from app.state import state

logger = logging.getLogger("mcp_server")

# The name passed to FastMCP is what shows up in an MCP client's server
# list (e.g. a "Connected servers" or connector settings screen) --
# keep it short and descriptive rather than matching the repo name.
mcp = FastMCP("anime-vibe-search")

MediaTypeFilter = Literal["ALL", "ANIME", "MANGA"]


@mcp.tool()
async def search_anime_manga_vibes(
    query: str,
    media_type: MediaTypeFilter = "ALL",
    limit: int = 10,
) -> dict:
    """Search for anime and manga by vibe, mood, or theme rather than by title.

    Use this when the user describes what they want to *feel* or a loose
    theme -- "something like a cozy slice-of-life with found family",
    "a revenge story that doesn't pull punches", "cyberpunk but hopeful"
    -- rather than naming a specific, already-known title. The query is
    embedded and the AniList catalog is ranked by semantic similarity
    (tie-broken by popularity), so results stay relevant even when no
    word in the query literally appears in a title's genres or tags.

    Args:
        query: A natural-language description of the desired vibe, mood,
            theme, or plot elements. Freeform sentences work better than
            a keyword list -- prefer "melancholic time-loop story with a
            small cast" over "time loop, sad".
        media_type: Restrict results to "ANIME", "MANGA", or "ALL"
            (default) for both.
        limit: Maximum number of results to return (1-50, default 10).

    Returns:
        A dict with `query`, `count`, and `results` -- a list of media
        objects (id, type, title_english, synopsis, genres, tags,
        popularity, cover_image_url, similarity, score), ordered by
        descending score. Identical shape to the REST endpoint's
        response body (see schemas.VibeSearchResponse).
    """
    limit = max(1, min(limit, 50))
    logger.info(
        "MCP tool call: query=%r media_type=%s limit=%s", query, media_type, limit
    )

    results = await vibe_search(state, query=query, limit=limit, media_type=media_type)
    return {"query": query, "count": len(results), "results": results}
