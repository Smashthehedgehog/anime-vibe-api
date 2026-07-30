"""Pydantic request/response models for POST /api/v1/search/vibe.

These exist purely for the REST surface: FastAPI uses them to validate
incoming JSON, coerce it into typed Python, and generate the OpenAPI
schema (visible at /docs). The MCP tool in mcp_server.py returns a plain
dict built from the same underlying vibe_search() call (see search.py) --
MCP tool results are JSON-RPC payloads, not FastAPI response models -- but
the *shape* of that dict matches VibeSearchResponse below, so REST and MCP
callers see the same fields either way.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MediaTypeFilter = Literal["ALL", "ANIME", "MANGA"]


class VibeSearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description=(
            "Natural-language description of the mood, vibe, or theme to "
            "search for, e.g. 'a slow-burn romance with found family "
            "themes' rather than a title or keyword list."
        ),
    )
    limit: int = Field(10, ge=1, le=50, description="Max number of results to return.")
    type: MediaTypeFilter = Field("ALL", description="Restrict results to ANIME, MANGA, or ALL.")


class MediaResult(BaseModel):
    id: int
    type: str
    title_english: Optional[str] = None
    synopsis: Optional[str] = None
    genres: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    popularity: int
    cover_image_url: Optional[str] = None
    similarity: float = Field(
        description="Raw cosine similarity between the query and this title's embedding, in [0, 1]."
    )
    score: float = Field(
        description=(
            "Final ranking score: 85% similarity + 15% log-normalized "
            "popularity. See match_media() in supabase/migrations/."
        )
    )


class VibeSearchResponse(BaseModel):
    query: str
    count: int
    results: list[MediaResult]
