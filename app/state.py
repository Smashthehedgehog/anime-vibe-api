"""Shared, process-wide handles for the embedding model and Supabase client.

Both the REST endpoint (app.py) and the MCP tool (mcp_server.py) need the
same SentenceTransformer instance and the same Supabase client. Loading
the model is the expensive part of a cold start (reading ~90MB of weights
off disk and into memory), so it happens exactly once, in FastAPI's
`lifespan` handler (see app.py), and every request afterwards -- REST or
MCP -- reads it from here instead of loading its own copy.

This is a plain module-level singleton rather than FastAPI's dependency
injection (`Depends(...)`) because the MCP tool functions in
mcp_server.py are invoked by the `mcp` SDK's own request handling, not by
FastAPI's router, so they can't receive a `Depends()`-injected value the
way an `@app.post(...)` route can.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sentence_transformers import SentenceTransformer
from supabase import Client


@dataclass
class AppState:
    model: Optional[SentenceTransformer] = None
    supabase: Optional[Client] = None

    def ready(self) -> bool:
        return self.model is not None and self.supabase is not None


# Populated by app.py's lifespan handler on startup, cleared on shutdown.
state = AppState()
