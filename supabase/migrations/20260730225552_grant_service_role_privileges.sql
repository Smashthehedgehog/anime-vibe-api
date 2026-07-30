-- On Supabase's hosted platform, the standard roles (anon, authenticated,
-- service_role) get sensible default grants on the public schema as part
-- of the platform's own project bootstrapping -- outside our migration
-- history entirely. A local `supabase start` instance doesn't get that
-- same bootstrapping, so tables created by our migrations end up owned
-- by `postgres` with no grants for the roles the app actually connects
-- as. Confirmed directly: SUPABASE_SERVICE_ROLE_KEY got
-- "permission denied for table media_metadata" (42501) against the
-- local instance despite working fine against the remote one.
--
-- Mirrors Supabase's own convention for these grants so local dev
-- matches what the hosted platform already does automatically.
grant usage on schema public to anon, authenticated, service_role;

grant all on all tables in schema public to postgres, anon, authenticated, service_role;
grant all on all sequences in schema public to postgres, anon, authenticated, service_role;
grant all on all routines in schema public to postgres, anon, authenticated, service_role;

alter default privileges in schema public grant all on tables to postgres, anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to postgres, anon, authenticated, service_role;
alter default privileges in schema public grant all on routines to postgres, anon, authenticated, service_role;
