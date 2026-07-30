-- Stage 4: portfolio API key management + per-key rate limiting.

create table if not exists api_keys (
    id uuid primary key default gen_random_uuid(),
    key_hash text not null unique,
    owner_label text not null,
    rate_limit_per_hour integer not null default 60,
    created_at timestamptz not null default now(),
    revoked_at timestamptz
);

comment on table api_keys is
    'Portfolio API keys. Only sha256(raw_key) is stored -- the raw key is '
    'returned once by POST /api/v1/keys/generate and never persisted.';
comment on column api_keys.key_hash is 'sha256(raw_key), hex-encoded.';
comment on column api_keys.revoked_at is
    'Set to disable a key without deleting its usage history.';

create table if not exists api_usage_logs (
    id bigint generated always as identity primary key,
    api_key_id uuid not null references api_keys(id) on delete cascade,
    endpoint text not null,
    created_at timestamptz not null default now()
);

comment on table api_usage_logs is
    'One row per successful authenticated request. Counted over a '
    'trailing 1-hour window to enforce api_keys.rate_limit_per_hour.';

-- Supports "count rows for this key in the last hour" efficiently.
create index if not exists api_usage_logs_key_time_idx
    on api_usage_logs (api_key_id, created_at desc);
