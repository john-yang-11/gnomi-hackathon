"""
Supabase storage — direct Postgres connection via psycopg2.
Bypasses PostgREST entirely so no schema cache issues.

Public surface:
    save_article(article)            -> existing articles table (unchanged behavior)
    enqueue_url(url, domain, ...)    -> crawl_queue (frontier)
    mark_queue_status(url, status)   -> update queue row + retry metadata
    claim_pending(limit)             -> pop URLs ready to crawl (respects backoff)
    log_event(event_type, ...)       -> append-only crawl_events
    save_duplicate(url, dup_type,..) -> duplicates table
    touch_domain(domain, ...)        -> domain_state throttle/health upsert
    due_domain(domain)               -> (ok, wait_seconds) per-domain throttle check
"""
import json
import ftfy
import psycopg2
from psycopg2.extras import Json

DATABASE_URL = "postgresql://postgres:7y3BG6bfCj0IXP8D@db.joprpfvwzmibmfexgkdp.supabase.co:5432/postgres"

_conn = None


def get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        _conn.autocommit = True
    return _conn


def _reset_conn():
    """Drop the cached connection so the next call reconnects."""
    global _conn
    _conn = None


def save_article(article: dict):
    """
    Upsert a single article into the existing `articles` table.
    Returns the article UUID (str) on success, or None on failure.
    (Previously returned bool — UUID is still truthy on success, so
     callers that only checked truthiness keep working.)
    """
    # Fix encoding issues (smart quotes, Windows-1252 artifacts)
    for field in ("main_text", "title", "summary"):
        if article.get(field):
            article[field] = ftfy.fix_text(article[field])

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO articles (
                    url, canonical_url, title, author, publish_date,
                    language, summary, main_text, headings,
                    source_domain, quality_score, quality_reasons
                ) VALUES (
                    %(url)s, %(canonical_url)s, %(title)s, %(author)s, %(publish_date)s,
                    %(language)s, %(summary)s, %(main_text)s, %(headings)s,
                    %(source_domain)s, %(quality_score)s, %(quality_reasons)s
                )
                ON CONFLICT (url) DO UPDATE SET
                    canonical_url  = EXCLUDED.canonical_url,
                    title          = EXCLUDED.title,
                    author         = EXCLUDED.author,
                    publish_date   = EXCLUDED.publish_date,
                    language       = EXCLUDED.language,
                    summary        = EXCLUDED.summary,
                    main_text      = EXCLUDED.main_text,
                    headings       = EXCLUDED.headings,
                    quality_score  = EXCLUDED.quality_score,
                    quality_reasons = EXCLUDED.quality_reasons,
                    crawled_at     = now()
                RETURNING id
            """, {
                **article,
                "headings": Json(article.get("headings", [])),
                "quality_reasons": Json(article.get("quality_reasons", {})),
            })
            return str(cur.fetchone()[0])
    except Exception as e:
        print(f"[DB] Failed to save {article.get('url')}: {e}")
        _reset_conn()
        return None


# ─────────────────────────────────────────────────────────────
# crawl_queue — the frontier
# ─────────────────────────────────────────────────────────────
def enqueue_url(url: str, source_domain: str, *, depth: int = 0,
                priority: int = 0, discovered_from: str | None = None) -> bool:
    """
    Add a URL to the crawl queue. Idempotent: ON CONFLICT DO NOTHING so the
    same URL is never queued twice (dedup at the frontier).
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crawl_queue (url, source_domain, depth, priority, discovered_from)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            """, (url, source_domain, depth, priority, discovered_from))
        return True
    except Exception as e:
        print(f"[DB] enqueue_url failed for {url}: {e}")
        _reset_conn()
        return False


def claim_pending(limit: int = 20) -> list[dict]:
    """
    Atomically claim up to `limit` queue rows that are pending and whose
    backoff window has elapsed. Marks them in_progress so concurrent workers
    don't grab the same rows (SKIP LOCKED). Returns list of {id,url,domain,depth,attempts}.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                WITH picked AS (
                    SELECT id FROM crawl_queue
                    WHERE status = 'pending' AND next_attempt_at <= now()
                    ORDER BY priority DESC, next_attempt_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE crawl_queue q
                   SET status = 'in_progress', updated_at = now()
                  FROM picked
                 WHERE q.id = picked.id
             RETURNING q.id, q.url, q.source_domain, q.depth, q.attempts
            """, (limit,))
            return [
                {"id": str(r[0]), "url": r[1], "source_domain": r[2],
                 "depth": r[3], "attempts": r[4]}
                for r in cur.fetchall()
            ]
    except Exception as e:
        print(f"[DB] claim_pending failed: {e}")
        _reset_conn()
        return []


def reclaim_stale(timeout_minutes: int = 10) -> int:
    """
    Reset `in_progress` rows that have been stuck longer than timeout_minutes
    back to `pending` so a crashed/interrupted run doesn't strand URLs.
    Returns the number of rows reclaimed.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE crawl_queue
                   SET status = 'pending', updated_at = now()
                 WHERE status = 'in_progress'
                   AND updated_at < now() - (%s || ' minutes')::interval
             RETURNING id
            """, (timeout_minutes,))
            return cur.rowcount
    except Exception as e:
        print(f"[DB] reclaim_stale failed: {e}")
        _reset_conn()
        return 0


def release_in_progress() -> int:
    """
    Reset ALL `in_progress` rows back to `pending`. Call on graceful shutdown
    so URLs claimed-but-not-finished (e.g. when --limit is hit) aren't stranded.
    Returns the number of rows released.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE crawl_queue
                   SET status = 'pending', updated_at = now()
                 WHERE status = 'in_progress'
             RETURNING id
            """)
            return cur.rowcount
    except Exception as e:
        print(f"[DB] release_in_progress failed: {e}")
        _reset_conn()
        return 0


def iter_articles_for_dedup(batch: int = 500):
    """
    Stream (url, main_text) for every stored article so a fresh DedupStore can
    be rehydrated at startup. Uses its own short-lived, non-autocommit
    connection because server-side (named) cursors require a transaction —
    the shared module connection runs in autocommit mode.
    """
    read_conn = None
    try:
        read_conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        # NOT autocommit: a transaction is required for a server-side cursor,
        # which streams rows in chunks instead of loading the whole table.
        with read_conn.cursor(name="dedup_loader") as cur:
            cur.itersize = batch
            cur.execute("SELECT url, main_text FROM articles WHERE main_text IS NOT NULL")
            for url, text in cur:
                yield url, text
    except Exception as e:
        print(f"[DB] iter_articles_for_dedup failed: {e}")
    finally:
        if read_conn is not None:
            read_conn.close()


def mark_queue_status(url: str, status: str, *, error: str | None = None,
                      article_id: str | None = None,
                      retry_backoff_seconds: int | None = None,
                      defer_seconds: int | None = None) -> bool:
    """
    Update a queue row's status and retry metadata.
      - status='failed' with retry_backoff_seconds: increments attempts and
        reschedules to pending if under max_attempts, else leaves it failed.
      - status='pending' with defer_seconds: push next_attempt_at forward
        WITHOUT counting an attempt (used when a domain is circuit-broken —
        the URL isn't at fault, so don't burn its retry budget). Prevents the
        claim→backoff→requeue livelock.
      - status='done': links the resulting article_id.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if status == "failed" and retry_backoff_seconds is not None:
                # Increment attempts; requeue with backoff unless we've hit max.
                cur.execute("""
                    UPDATE crawl_queue SET
                        attempts        = attempts + 1,
                        last_error      = %s,
                        updated_at      = now(),
                        status          = CASE WHEN attempts + 1 >= max_attempts
                                               THEN 'failed' ELSE 'pending' END,
                        next_attempt_at = now() + (%s || ' seconds')::interval
                    WHERE url = %s
                """, (error, retry_backoff_seconds, url))
            elif status == "pending" and defer_seconds is not None:
                # Defer without counting an attempt (domain backoff).
                cur.execute("""
                    UPDATE crawl_queue SET
                        status          = 'pending',
                        updated_at      = now(),
                        next_attempt_at = now() + (%s || ' seconds')::interval
                    WHERE url = %s
                """, (defer_seconds, url))
            else:
                cur.execute("""
                    UPDATE crawl_queue SET
                        status     = %s,
                        last_error = COALESCE(%s, last_error),
                        article_id = COALESCE(%s, article_id),
                        updated_at = now()
                    WHERE url = %s
                """, (status, error, article_id, url))
        return True
    except Exception as e:
        print(f"[DB] mark_queue_status failed for {url}: {e}")
        _reset_conn()
        return False


# ─────────────────────────────────────────────────────────────
# crawl_events — append-only log
# ─────────────────────────────────────────────────────────────
def log_event(event_type: str, *, url: str | None = None,
              source_domain: str | None = None, queue_id: str | None = None,
              article_id: str | None = None, status_code: int | None = None,
              details: dict | None = None) -> bool:
    """Append one row to crawl_events. Never raises into the crawl loop."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crawl_events
                    (event_type, url, source_domain, queue_id, article_id, status_code, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (event_type, url, source_domain, queue_id, article_id,
                  status_code, Json(details or {})))
        return True
    except Exception as e:
        print(f"[DB] log_event failed ({event_type}): {e}")
        _reset_conn()
        return False


# ─────────────────────────────────────────────────────────────
# duplicates
# ─────────────────────────────────────────────────────────────
def save_duplicate(url: str, dup_type: str, *, canonical_url: str | None = None,
                   source_domain: str | None = None, duplicate_of: str | None = None,
                   original_article_id: str | None = None,
                   similarity: float | None = None, content_hash: str | None = None,
                   details: dict | None = None) -> bool:
    """Record a rejected duplicate. dup_type in {'url','exact_content','near_content'}."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO duplicates
                    (url, canonical_url, source_domain, dup_type, duplicate_of,
                     original_article_id, similarity, content_hash, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (url, canonical_url, source_domain, dup_type, duplicate_of,
                  original_article_id, similarity, content_hash, Json(details or {})))
        return True
    except Exception as e:
        print(f"[DB] save_duplicate failed for {url}: {e}")
        _reset_conn()
        return False


# ─────────────────────────────────────────────────────────────
# domain_state — per-domain throttling + health
# ─────────────────────────────────────────────────────────────
def touch_domain(domain: str, *, fetched: bool = False, blocked: bool = False,
                 crawl_delay: float | None = None,
                 block_threshold: int = 4,
                 block_backoff_seconds: int = 300) -> bool:
    """
    Upsert domain_state after a fetch. Bumps counters, records last_fetch_at,
    and trips a CIRCUIT BREAKER: once a domain hits `block_threshold`
    consecutive blocks, set blocked_until = now() + block_backoff_seconds so
    due_domain() backs the whole domain off. A successful fetch clears it.
    """
    b = bool(blocked)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO domain_state (domain, crawl_delay, last_fetch_at,
                                          total_fetched, total_blocked,
                                          consecutive_failures)
                VALUES (%(domain)s, COALESCE(%(delay)s, 1.0), now(),
                        %(f)s, %(b)s, %(b)s)
                ON CONFLICT (domain) DO UPDATE SET
                    last_fetch_at        = now(),
                    crawl_delay          = COALESCE(%(delay)s, domain_state.crawl_delay),
                    total_fetched        = domain_state.total_fetched + %(f)s,
                    total_blocked        = domain_state.total_blocked + %(b)s,
                    consecutive_failures = CASE WHEN %(blocked)s
                                                THEN domain_state.consecutive_failures + 1
                                                ELSE 0 END,
                    blocked_until        = CASE
                        -- success clears the breaker
                        WHEN NOT %(blocked)s THEN NULL
                        -- threshold reached: back the domain off
                        WHEN domain_state.consecutive_failures + 1 >= %(threshold)s
                            THEN now() + (%(backoff)s || ' seconds')::interval
                        -- blocked but under threshold: leave any existing window
                        ELSE domain_state.blocked_until
                    END,
                    updated_at           = now()
            """, {
                "domain": domain,
                "delay": crawl_delay,
                "f": 1 if fetched else 0,
                "b": 1 if b else 0,
                "blocked": b,
                "threshold": block_threshold,
                "backoff": block_backoff_seconds,
            })
        return True
    except Exception as e:
        print(f"[DB] touch_domain failed for {domain}: {e}")
        _reset_conn()
        return False


def due_domain(domain: str) -> tuple[bool, float]:
    """
    Per-domain throttle check. Returns (ok_to_fetch, wait_seconds).
    Honors crawl_delay since last_fetch_at and any blocked_until backoff.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    GREATEST(
                        COALESCE(EXTRACT(EPOCH FROM (blocked_until - now())), 0),
                        COALESCE(crawl_delay - EXTRACT(EPOCH FROM (now() - last_fetch_at)), 0)
                    ) AS wait_seconds
                FROM domain_state WHERE domain = %s
            """, (domain,))
            row = cur.fetchone()
        if row is None:
            return True, 0.0  # never seen this domain — fetch freely
        wait = max(float(row[0]), 0.0)
        return (wait <= 0.0, wait)
    except Exception as e:
        print(f"[DB] due_domain failed for {domain}: {e}")
        _reset_conn()
        return True, 0.0
