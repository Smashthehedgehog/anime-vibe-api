"""Stage 4: API key auth + rate limiting.

TODO:
- FastAPI dependency validating X-API-Key (sha256 lookup in api_keys)
- rate limit via recent-row count in api_usage_logs
- reuse as a connection guard for the MCP SSE endpoint
- POST /api/v1/keys/generate, protected by ADMIN_MASTER_TOKEN
"""
