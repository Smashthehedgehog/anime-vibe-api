"""Stage 3: FastAPI app.

TODO:
- lifespan: load SentenceTransformer once at startup
- POST /api/v1/search/vibe -> vectorize, call match_media RPC
- CORS locked to portfolio domains
- mount mcp_server (stage 3) and auth deps (stage 4)
"""
