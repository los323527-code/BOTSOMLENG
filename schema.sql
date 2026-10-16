create table if not exists public.connected_bots (
  bot_key text primary key,
  owner_tg_id bigint not null,
  bot_token_enc text not null,
  bot_username text not null,
  webhook_secret_hash text not null,
  created_at timestamptz not null default now()
);

alter table public.connected_bots enable row level security;

-- Do not create public policies for this table.
-- Access is performed only by the server using the Supabase service-role key.
