-- CleanCrawl — crawl infrastructure migration
-- Additive only: does NOT touch the existing `articles` table.
-- Adds: crawl_queue, crawl_events, duplicates, domain_state.

-- ─────────────────────────────────────────────────────────────
-- 1. crawl_queue — the frontier. One row per URL to crawl.
--    Holds status + retry metadata so the crawler can resume,
--    retry with backoff, and never re-enqueue the same URL.
-- ─────────────────────────────────────────────────────────────
create table if not exists crawl_queue (
  id              uuid primary key default gen_random_uuid(),
  url             text unique not null,          -- normalized/canonical URL
  source_domain   text not null,
  status          text not null default 'pending'
                    check (status in ('pending','in_progress','done',
                                      'failed','skipped','blocked')),
  priority        int  not null default 0,        -- higher = crawl sooner
  depth           int  not null default 0,        -- link depth from seed
  attempts        int  not null default 0,        -- retry counter
  max_attempts    int  not null default 3,
  next_attempt_at timestamptz not null default now(),  -- backoff schedule
  last_error      text,
  discovered_from text,                            -- referrer URL (nullable)
  article_id      uuid references articles(id) on delete set null,
  enqueued_at     timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- Queue pickup: "give me pending URLs whose backoff has elapsed, best priority first"
create index if not exists crawl_queue_pickup_idx
  on crawl_queue (status, next_attempt_at, priority desc);
create index if not exists crawl_queue_domain_idx   on crawl_queue (source_domain);
create index if not exists crawl_queue_status_idx   on crawl_queue (status);

-- ─────────────────────────────────────────────────────────────
-- 2. crawl_events — append-only audit log. One row per event.
--    bigint identity (not uuid): high-volume, append-only, ordered.
--    `details` jsonb absorbs anything event-specific.
-- ─────────────────────────────────────────────────────────────
create table if not exists crawl_events (
  id            bigint generated always as identity primary key,
  event_type    text not null,                    -- see allowed values below
  url           text,
  source_domain text,
  queue_id      uuid references crawl_queue(id) on delete set null,
  article_id    uuid references articles(id)     on delete set null,
  status_code   int,                              -- HTTP status when relevant
  details       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);

-- event_type vocabulary (kept as comment, not a CHECK, so new types don't need a migration):
--   enqueued, fetch_started, fetch_success, fetch_failed, blocked,
--   classified_skip, extracted, low_quality, duplicate, saved,
--   retry_scheduled, gave_up, error
create index if not exists crawl_events_type_idx     on crawl_events (event_type);
create index if not exists crawl_events_domain_idx   on crawl_events (source_domain);
create index if not exists crawl_events_created_idx  on crawl_events (created_at desc);
create index if not exists crawl_events_queue_idx    on crawl_events (queue_id);
-- Composite for the common "events of type X for domain Y" monitoring query
create index if not exists crawl_events_domain_type_idx
  on crawl_events (source_domain, event_type);

-- ─────────────────────────────────────────────────────────────
-- 3. duplicates — one row per rejected duplicate occurrence.
--    Records WHY it was a dupe and what it duplicated, so you can
--    audit dedup behavior and report syndication overlap.
-- ─────────────────────────────────────────────────────────────
create table if not exists duplicates (
  id                  uuid primary key default gen_random_uuid(),
  url                 text not null,               -- the URL we rejected
  canonical_url       text,
  source_domain       text,
  dup_type            text not null
                        check (dup_type in ('url','exact_content','near_content')),
  duplicate_of        text,                        -- URL/canonical it matched
  original_article_id uuid references articles(id) on delete set null,
  similarity          float,                       -- MinHash jaccard for near-dupe
  content_hash        text,                        -- sha256 of normalized text
  details             jsonb not null default '{}'::jsonb,
  detected_at         timestamptz not null default now()
);

create index if not exists duplicates_type_idx    on duplicates (dup_type);
create index if not exists duplicates_hash_idx     on duplicates (content_hash);
create index if not exists duplicates_domain_idx   on duplicates (source_domain);
create index if not exists duplicates_origin_idx   on duplicates (original_article_id);

-- ─────────────────────────────────────────────────────────────
-- 4. domain_state — one row per domain. Per-domain throttling +
--    health / circuit-breaking. The crawler reads crawl_delay and
--    last_fetch_at to space requests; blocked_until backs off a
--    domain that is actively blocking us.
-- ─────────────────────────────────────────────────────────────
create table if not exists domain_state (
  domain               text primary key,
  crawl_delay          float not null default 1.0,   -- seconds between fetches
  concurrent_limit     int   not null default 2,
  last_fetch_at        timestamptz,
  robots_checked_at    timestamptz,
  consecutive_failures int   not null default 0,
  blocked_until        timestamptz,                  -- backoff window
  total_fetched        int   not null default 0,
  total_blocked        int   not null default 0,
  details              jsonb not null default '{}'::jsonb,
  updated_at           timestamptz not null default now()
);

create index if not exists domain_state_blocked_idx on domain_state (blocked_until);
