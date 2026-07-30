"""FastAPI entry point.

Exposes two independent protocols from one process:

  * A conventional REST endpoint, `POST /api/v1/search/vibe`, for a
    browser-based frontend.
  * An MCP server, mounted at `/sse` + `/messages/`, so LLM agents can
    call the same search as a tool over a live connection instead of a
    one-shot HTTP request. See mcp_server.py for what that protocol
    actually looks like on the wire, and docs/MCP.md for how to connect
    a client to it.

Both protocols share one SentenceTransformer instance and one Supabase
client (see state.py), loaded once at process startup in `lifespan`
below rather than per-request or lazily on first use -- loading the
embedding model is the slow part of a cold start, so doing it eagerly at
startup is what keeps every request (REST or MCP) at sub-150ms.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sentence_transformers import SentenceTransformer
from supabase import create_client

from app import recommend_agent
from app.auth import ApiKeyASGIGuard, ApiKeyRecord, create_api_key, require_admin, require_api_key
from app.mcp_server import mcp
from app.schemas import (
    GenerateKeyRequest,
    GenerateKeyResponse,
    RecommendRequest,
    RecommendResponse,
    VibeSearchRequest,
    VibeSearchResponse,
)
from app.search import vibe_search
from app.state import state

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("app")

EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on process start, once on process stop.

    Loading the SentenceTransformer model from disk takes roughly a
    second. Doing that here -- instead of on the first incoming request
    -- means every caller gets consistent latency instead of the first
    request (REST or MCP) silently eating the load time.
    """
    logger.info("Loading embedding model %s", EMBEDDING_MODEL_NAME)
    state.model = SentenceTransformer(EMBEDDING_MODEL_NAME)

    logger.info("Connecting to Supabase")
    state.supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    )

    logger.info("Startup complete, ready to serve")
    yield

    state.model = None
    state.supabase = None


app = FastAPI(
    title="Anime Vibe Recommender",
    description=(
        "Semantic vibe-based search over the AniList anime/manga catalog, "
        "served as both a REST API and an MCP tool."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Strict allowlist: only the specific portfolio/frontend origins listed in
# CORS_ALLOWED_ORIGINS (comma-separated) can call this from a browser.
# Deliberately not "*" -- this API is meant to be shown off publicly, and
# the stage-4 API-key layer is the other half of making that safe, not a
# replacement for a tight CORS policy.
_allowed_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["POST"],
    allow_headers=["Content-Type", "X-API-Key", "X-Admin-Token"],
)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    return {"status": "ok" if state.ready() else "starting"}


@app.post("/api/v1/search/vibe", response_model=VibeSearchResponse, tags=["search"])
async def search_vibe(
    request: VibeSearchRequest, _key: ApiKeyRecord = Depends(require_api_key)
) -> VibeSearchResponse:
    """Semantic vibe search over anime/manga.

    Embeds `query`, calls the `match_media` Postgres RPC, and returns
    results ranked by a blend of semantic similarity and popularity. This
    is the REST twin of the MCP tool `search_anime_manga_vibes` in
    mcp_server.py -- both call the same vibe_search() helper (search.py),
    so they always agree.

    Requires a valid `X-API-Key` header (see auth.py); `require_api_key`
    also enforces that key's per-hour rate limit before this body runs.
    """
    try:
        results = await vibe_search(
            state, query=request.query, limit=request.limit, media_type=request.type
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return VibeSearchResponse(query=request.query, count=len(results), results=results)


@app.post("/api/v1/recommend", response_model=RecommendResponse, tags=["search"])
async def recommend(
    request: RecommendRequest,
    _key: ApiKeyRecord = Depends(require_api_key),
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> RecommendResponse:
    """Method 2: an LLM-driven recommendation, as opposed to method 1's
    raw ranked list (POST /api/v1/search/vibe).

    Unlike vibe_search(), this doesn't call the database directly -- it
    hands the caller's own API key to recommend_agent.get_recommendation(),
    which connects to *this same server's* MCP endpoint as a genuine
    client and lets an LLM (Groq) decide how to use the
    search_anime_manga_vibes tool. See recommend_agent.py for why that
    indirection is deliberate rather than just calling vibe_search()
    in-process. Requires `require_api_key` same as search (this route's
    own call), and the agent's internal tool calls are metered against
    that same key too -- there's no separate bypass credential.
    """
    try:
        result = await recommend_agent.get_recommendation(
            api_key=x_api_key, vibe=request.vibe, media_type=request.type
        )
    except Exception as exc:  # noqa: BLE001 -- surface any agent/Groq/MCP failure as a clean 502
        logger.exception("Recommendation agent failed")
        raise HTTPException(status_code=502, detail=f"Recommendation agent failed: {exc}") from exc

    return RecommendResponse(
        vibe=request.vibe,
        explanation=result["explanation"],
        media=result["media"],
        tool_calls=result["tool_calls"],
    )


@app.post(
    "/api/v1/keys/generate",
    response_model=GenerateKeyResponse,
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)
async def generate_key(request: GenerateKeyRequest) -> GenerateKeyResponse:
    """Provision a new API key. Requires `X-Admin-Token: $ADMIN_MASTER_TOKEN`.

    Meant to be called from your own signup/provisioning flow (e.g. a
    server-side handler on your portfolio site when a visitor requests a
    demo key), not directly from public frontend JavaScript -- doing that
    would ship ADMIN_MASTER_TOKEN to every visitor's browser, which
    defeats the point of gating this endpoint at all. For a
    fully-public "click to get a key" flow, swap `require_admin` for
    Supabase Auth JWT verification instead.
    """
    raw_key = await create_api_key(
        owner_label=request.owner_label, rate_limit_per_hour=request.rate_limit_per_hour
    )
    return GenerateKeyResponse(
        api_key=raw_key,
        owner_label=request.owner_label,
        rate_limit_per_hour=request.rate_limit_per_hour,
    )


# --- MCP mount ---------------------------------------------------------
# mcp.sse_app() returns a small Starlette application implementing the
# SSE-transport routes MCP clients expect: GET /sse (the long-lived
# stream) and POST /messages/ (where clients send JSON-RPC requests).
# See mcp_server.py's module docstring for what that pair of routes is
# actually doing.
#
# Mounting at "/" makes those routes resolve to exactly /sse and
# /messages/ at the app's root, matching what's documented for clients
# in docs/MCP.md. The routes declared above (/healthz,
# /api/v1/search/vibe, /api/v1/recommend, /api/v1/keys/generate) are
# registered on `app` directly and are matched before falling through to
# this mount, so there's no collision. Note that /api/v1/recommend
# itself calls back into this same mount as an MCP client -- see
# recommend_agent.py.
#
# ApiKeyASGIGuard wraps the mounted sub-app with the same X-API-Key
# check `require_api_key` does for REST -- see auth.py's module
# docstring for why the MCP surface needs a different enforcement
# mechanism than a FastAPI Depends().
app.mount("/", ApiKeyASGIGuard(mcp.sse_app()))
