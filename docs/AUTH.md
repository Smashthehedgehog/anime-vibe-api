# API keys & rate limiting

Every request to `POST /api/v1/search/vibe` and the MCP server (`/sse`)
must carry a valid `X-API-Key` header. This exists so the API can be
linked from a public portfolio without a stranger (or a bot) running up
the Supabase bill or DoS-ing the embedding model. See `app/auth.py` for
the implementation; this doc covers the operational side — how to
actually get and manage keys.

## Issuing a key

```bash
curl -X POST https://<your-deployment>/api/v1/keys/generate \
  -H "X-Admin-Token: $ADMIN_MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "portfolio-visitor@example.com", "rate_limit_per_hour": 60}'
```

Response (only time the raw key is ever shown):

```json
{
  "api_key": "z3W4eIglpc9BXbVVB3JNFn6UOZkjYuk8NpJdcwaW9TM",
  "owner_label": "portfolio-visitor@example.com",
  "rate_limit_per_hour": 60
}
```

`ADMIN_MASTER_TOKEN` is a single shared secret you set as an environment
variable (see `.env.example`) — treat it like a root password. **Never
call `/api/v1/keys/generate` from public frontend JavaScript**; that
would ship the admin token to every visitor's browser. Call it from a
server-side handler (a Vercel/Netlify function, your own backend, or
just your own terminal) when provisioning a demo key for someone.

## Revoking a key

There's no revoke endpoint yet — do it directly in the Supabase table
editor or SQL editor:

```sql
update api_keys set revoked_at = now() where key_hash = '<sha256 hex of the key>';
```

(You generally won't have the raw key at revocation time — look the row
up by `owner_label` instead, or keep a private record of which hash
belongs to which raw key when you issue it.)

## How the rate limit works

Each key has its own `rate_limit_per_hour` (set at creation, default 60).
On every request, `auth.py` counts that key's rows in `api_usage_logs`
created within the trailing 60 minutes (a sliding window, not a
fixed-clock-hour bucket) and rejects with `429` if the count is already
at the limit. A row is only logged for requests that pass the check —
rejected requests don't count against the limit, so a client already at
capacity can keep polling without digging itself deeper (see the
trade-off note in `app/auth.py`'s module docstring).

## Why a shared admin token instead of Supabase Auth

The original design considered gating `/api/v1/keys/generate` behind
Supabase Auth (verifying a JWT from a real user login) instead of one
shared token. For a single-operator portfolio API — you provisioning a
handful of demo keys yourself — a shared admin token is simpler and has
no user-management surface to build. If this ever grows into something
where visitors self-serve a key by creating an account, swap
`require_admin` in `app/auth.py` for JWT verification at that point;
`create_api_key()` and the rest of the flow don't need to change.
