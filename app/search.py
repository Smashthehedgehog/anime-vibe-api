"""Core vibe-search logic, shared by the REST endpoint and the MCP tool.

Both surfaces (app.py's POST /api/v1/search/vibe, and mcp_server.py's
`search_anime_manga_vibes` tool) do exactly the same three steps: embed
the query text, call the `match_media` Postgres RPC (see
supabase/migrations/20260730014130_hybrid_search.sql), and hand back the
rows. Keeping that logic in one place means the two surfaces can never
drift out of sync with each other -- an agent calling the MCP tool and a
browser calling the REST endpoint with the same query always get the
same answer.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from app.state import AppState

MediaTypeFilter = Literal["ANIME", "MANGA"]


async def vibe_search(
    state: AppState,
    query: str,
    limit: int,
    media_type: MediaTypeFilter,
    match_threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """Embed `query` and return the top `limit` matches from match_media().

    Raises RuntimeError if called before the model/Supabase client have
    finished loading (see app.py's lifespan handler) -- this shouldn't
    happen in normal operation since FastAPI won't start serving requests
    until lifespan startup completes, but it's checked explicitly since
    this function is also reachable from the MCP path, which has its own
    request lifecycle.
    """
    if not state.ready():
        raise RuntimeError("Search backend not initialized yet")

    # TextEmbedding.embed() and the supabase-py client are both
    # synchronous/blocking calls. Running them via asyncio.to_thread keeps
    # the event loop free to serve other concurrent requests (REST or MCP)
    # while this one waits on CPU-bound encoding or network I/O.
    # embed() takes an iterable and returns a generator, even for a
    # single string -- there's no single-text convenience method.
    def _encode() -> list[float]:
        return next(iter(state.model.embed([query]))).tolist()

    embedding = await asyncio.to_thread(_encode)

    def _call_rpc() -> list[dict[str, Any]]:
        response = state.supabase.rpc(
            "match_media",
            {
                "query_embedding": embedding,
                "match_threshold": match_threshold,
                "match_count": limit,
                "media_type": media_type,
            },
        ).execute()
        return response.data or []

    return await asyncio.to_thread(_call_rpc)
