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


class RecommendRequest(BaseModel):
    vibe: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Natural-language description of the mood, vibe, or theme you want a recommendation for.",
    )
    type: MediaTypeFilter = Field("ALL", description="Restrict the recommendation to ANIME, MANGA, or ALL.")


class ToolCallLogEntry(BaseModel):
    tool: str
    arguments: dict


class RecommendResponse(BaseModel):
    vibe: str
    explanation: str = Field(description="The agent's natural-language case for this pick.")
    media: Optional[MediaResult] = Field(
        default=None,
        description=(
            "The recommended title, if the agent settled on one it actually saw via the "
            "search tool. Null if it never called the tool, or its final answer didn't "
            "reference a real candidate."
        ),
    )
    tool_calls: list[ToolCallLogEntry] = Field(
        default_factory=list, description="Every search_anime_manga_vibes call the agent made while deciding."
    )


class GenerateKeyRequest(BaseModel):
    owner_label: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-readable label for who/what this key is for, e.g. an email address or portfolio visitor name.",
    )
    rate_limit_per_hour: int = Field(
        60, ge=1, le=10_000, description="Requests this key may make per trailing 60-minute window."
    )


class GenerateKeyResponse(BaseModel):
    api_key: str = Field(description="The raw key. Shown once, here -- it is never stored or retrievable again.")
    owner_label: str
    rate_limit_per_hour: int
