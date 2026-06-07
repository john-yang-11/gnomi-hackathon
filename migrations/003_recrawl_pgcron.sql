-- CleanCrawl — activate freshness-aware recrawl entirely inside the database.
-- pg_cron periodically flips 'done' queue rows back to 'pending' once their
-- content-type freshness window has elapsed. The crawler (run on any schedule)
-- then drains them; dedup handles unchanged pages → 'duplicate', changed → new.
--
-- No application code is involved in SCHEDULING — this runs in Supabase forever.

-- 1. Enable pg_cron (idempotent)
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- 2. The re-enqueue logic, as a function so the cron job stays readable.
--    Freshness windows per content_type: news changes fast, wikis slowly.
CREATE OR REPLACE FUNCTION reenqueue_stale_articles()
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    n integer;
BEGIN
    WITH due AS (
        UPDATE crawl_queue q
           SET status = 'pending',
               next_attempt_at = now(),
               updated_at = now()
          FROM articles a
         WHERE q.article_id = a.id
           AND q.status = 'done'
           AND q.updated_at < now() - (
                 CASE a.content_type
                     WHEN 'news' THEN interval '6 hours'
                     WHEN 'blog' THEN interval '48 hours'
                     WHEN 'wiki' THEN interval '168 hours'
                     ELSE interval '24 hours'
                 END
               )
       RETURNING q.id
    )
    SELECT count(*) INTO n FROM due;
    RETURN n;
END;
$$;

-- 3. Schedule it hourly. (Adjust the cron expression to taste.)
--    Unschedule any prior version first so re-running this file is safe.
SELECT cron.unschedule('cleancrawl-recrawl')
  WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'cleancrawl-recrawl');

SELECT cron.schedule(
    'cleancrawl-recrawl',
    '0 * * * *',                       -- top of every hour
    $$ SELECT reenqueue_stale_articles(); $$
);
