"""Stage 4: API key authentication + per-key rate limiting.

This API is meant to be shown off publicly on a portfolio, which means it
needs to survive being hammered by strangers (or bots) without racking up
a Supabase/embedding-model bill or falling over. The approach here is
deliberately simple:

  * Every caller must present a raw key via the `X-API-Key` header.
  * We never store that raw key -- only sha256(raw_key) -- so a database
    leak doesn't hand out working credentials (mirrors how you'd store a
    password, even though this isn't one).
  * Each key has its own `rate_limit_per_hour`. We enforce it by counting
    that key's rows in `api_usage_logs` from the trailing 60 minutes --
    no separate counter/cache to keep in sync, Postgres is the source of
    truth. This is a sliding window, not a fixed per-clock-hour bucket:
    "60 requests in the last hour" at any instant, not "60 requests since
    the top of the hour."

--------------------------------------------------------------------------
Two enforcement points, one shared check
--------------------------------------------------------------------------
This module is used from two different places that don't share a common
request-handling framework:

  1. `require_api_key` -- a FastAPI dependency, attached with `Depends()`
     to the REST route in app.py. Standard FastAPI stuff: raises
     HTTPException, shows up as a documented header requirement in
     /docs.
  2. `ApiKeyASGIGuard` -- wraps the *mounted* MCP SSE app. MCP's SSE
     transport is two raw ASGI routes (GET /sse, POST /messages/) served
     by a Starlette sub-application (see mcp_server.py / app.py), not a
     FastAPI route function -- there's nothing to attach `Depends()` to.
     This class sits in front of that sub-app at the ASGI level and
     performs the identical check before letting a request through, so
     an unauthenticated client can't even open the /sse stream.

Both call `_validate_and_meter()`, so REST and MCP enforce identically:
same key lookup, same rate limit math, same usage logging. Only the
error-response plumbing differs, because FastAPI exception handling and
raw ASGI responses work differently.

Trade-off worth knowing about: a *rejected* request (bad key, or over the
rate limit) is not logged to `api_usage_logs`. That keeps a client who's
already over their limit from digging themselves deeper by retrying, but
it also means the rate limit only counts *successful* calls -- a burst of
rapid-fire rejected requests costs nothing but the lookup query. For a
portfolio demo this trade favors simplicity; a production system fronting
something expensive would want to log attempts too.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException, status
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.state import state

logger = logging.getLogger("auth")

RATE_LIMIT_WINDOW = timedelta(hours=1)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass
class ApiKeyRecord:
    id: str
    owner_label: str
    rate_limit_per_hour: int


class InvalidApiKey(Exception):
    """Missing header, unknown key, or a revoked key."""


class RateLimitExceeded(Exception):
    """Key is valid but has used up its trailing-hour quota."""


class AuthBackendNotReady(Exception):
    """Called before the lifespan handler finished loading state.supabase."""


async def _validate_and_meter(raw_key: Optional[str], *, endpoint: str) -> ApiKeyRecord:
    """Look up `raw_key`, enforce its rate limit, and log this call.

    Shared by both enforcement points described in the module docstring.
    Raises InvalidApiKey, RateLimitExceeded, or AuthBackendNotReady --
    callers translate those into the right response for their protocol.
    """
    if not raw_key:
        raise InvalidApiKey("Missing X-API-Key header")

    if not state.ready():
        raise AuthBackendNotReady("Auth backend not initialized yet")

    key_hash = _hash_key(raw_key)

    def _lookup_key():
        return (
            state.supabase.table("api_keys")
            .select("id, owner_label, rate_limit_per_hour, revoked_at")
            .eq("key_hash", key_hash)
            .is_("revoked_at", "null")
            .limit(1)
            .execute()
        )

    lookup_response = await asyncio.to_thread(_lookup_key)
    rows = lookup_response.data or []
    if not rows:
        raise InvalidApiKey("Invalid or revoked API key")

    row = rows[0]
    record = ApiKeyRecord(
        id=row["id"],
        owner_label=row["owner_label"],
        rate_limit_per_hour=row["rate_limit_per_hour"],
    )

    cutoff = (datetime.now(timezone.utc) - RATE_LIMIT_WINDOW).isoformat()

    def _count_recent_usage():
        return (
            state.supabase.table("api_usage_logs")
            .select("id", count="exact")
            .eq("api_key_id", record.id)
            .gte("created_at", cutoff)
            .execute()
        )

    usage_response = await asyncio.to_thread(_count_recent_usage)
    recent_count = usage_response.count or 0

    if recent_count >= record.rate_limit_per_hour:
        logger.warning(
            "Rate limit exceeded: key=%s owner=%s count=%s limit=%s",
            record.id,
            record.owner_label,
            recent_count,
            record.rate_limit_per_hour,
        )
        raise RateLimitExceeded(
            f"Rate limit exceeded: {record.rate_limit_per_hour} requests/hour"
        )

    def _log_usage():
        return (
            state.supabase.table("api_usage_logs")
            .insert({"api_key_id": record.id, "endpoint": endpoint})
            .execute()
        )

    await asyncio.to_thread(_log_usage)

    return record


# --- REST enforcement: a normal FastAPI dependency -------------------------


async def require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> ApiKeyRecord:
    """FastAPI dependency: attach with `Depends(require_api_key)`.

    Validates the caller's key and meters this call against its rate
    limit, in one round trip through `_validate_and_meter`. See the
    module docstring for why this exists alongside `ApiKeyASGIGuard`.
    """
    try:
        return await _validate_and_meter(x_api_key, endpoint="/api/v1/search/vibe")
    except InvalidApiKey as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except AuthBackendNotReady as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- a Supabase/network hiccup here shouldn't surface as a bare 500
        logger.exception("Unexpected error validating API key")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth backend temporarily unavailable") from exc


# --- MCP SSE enforcement: an ASGI-level guard -------------------------------


class ApiKeyASGIGuard:
    """Wraps an ASGI app (the mounted MCP server) with the same API-key
    check `require_api_key` does for REST.

    MCP's SSE transport is two bare ASGI routes glued together by the
    `mcp` SDK (GET /sse, POST /messages/), not a FastAPI route function
    -- there's no request handler to attach `Depends()` to, and no
    single "the request" whose body FastAPI has already parsed for us.
    Instead this class sits directly in front of the mounted sub-app: it
    reads the `X-API-Key` header off the raw ASGI scope, runs the exact
    same `_validate_and_meter` check the REST dependency uses, and either
    forwards the request to the real MCP app or short-circuits with a
    JSON error response -- before the client's connection ever reaches
    the SSE stream or the JSON-RPC message handler.

    Non-HTTP scopes (there aren't any in this app, but ASGI apps should
    handle `lifespan`/`websocket` scope types gracefully) pass straight
    through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        raw_key = request.headers.get("x-api-key")

        try:
            await _validate_and_meter(raw_key, endpoint=request.url.path)
        except InvalidApiKey as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=401)
            await response(scope, receive, send)
            return
        except RateLimitExceeded as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=429)
            await response(scope, receive, send)
            return
        except AuthBackendNotReady as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=503)
            await response(scope, receive, send)
            return
        except Exception:  # noqa: BLE001 -- a Supabase/network hiccup here shouldn't surface as a bare 500
            logger.exception("Unexpected error validating API key (ASGI guard)")
            response = JSONResponse({"detail": "Auth backend temporarily unavailable"}, status_code=503)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# --- Admin-protected key issuance ------------------------------------------


async def require_admin(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
) -> None:
    """Guards POST /api/v1/keys/generate.

    Deliberately not the same mechanism as `require_api_key`: issuing new
    keys is an admin action gated by a single shared secret
    (`ADMIN_MASTER_TOKEN`, set as a Render/local env var), not something
    any existing API key should be able to do to itself.
    `secrets.compare_digest` avoids leaking the token's value through
    response-time timing differences.
    """
    expected = os.environ.get("ADMIN_MASTER_TOKEN")
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


async def create_api_key(owner_label: str, rate_limit_per_hour: int) -> str:
    """Generate a new API key, store only its hash, and return the raw key.

    The raw key is returned exactly once, here, at creation time -- it is
    never written to the database or logged. If it's lost, the only fix
    is generating a new one.
    """
    raw_key = secrets.token_urlsafe(32)
    key_hash = _hash_key(raw_key)

    def _insert():
        return (
            state.supabase.table("api_keys")
            .insert(
                {
                    "key_hash": key_hash,
                    "owner_label": owner_label,
                    "rate_limit_per_hour": rate_limit_per_hour,
                }
            )
            .execute()
        )

    await asyncio.to_thread(_insert)
    return raw_key
