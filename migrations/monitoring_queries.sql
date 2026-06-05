-- CleanCrawl monitoring queries (read-only, dashboard-friendly)

-- 1. Crawl success rate (overall) — from the event log
SELECT
  count(*) FILTER (WHERE event_type = 'saved')                              AS saved,
  count(*) FILTER (WHERE event_type IN ('fetch_failed','blocked','gave_up')) AS failures,
  count(*) FILTER (WHERE event_type = 'fetch_started')                       AS attempts,
  round(
    100.0 * count(*) FILTER (WHERE event_type = 'saved')
    / NULLIF(count(*) FILTER (WHERE event_type = 'fetch_started'), 0), 1
  ) AS success_pct
FROM crawl_events;

-- 2. Blocked pages by domain
SELECT source_domain,
       count(*)                                    AS blocked_count,
       jsonb_agg(DISTINCT details->>'block_reason') AS reasons
FROM crawl_events
WHERE event_type = 'blocked'
GROUP BY source_domain
ORDER BY blocked_count DESC;

-- 3. Duplicate count by type
SELECT dup_type,
       count(*)            AS n,
       round(avg(similarity)::numeric, 3) AS avg_similarity
FROM duplicates
GROUP BY dup_type
ORDER BY n DESC;

-- 4. Average quality score (overall + per domain)
SELECT source_domain,
       count(*)                              AS articles,
       round(avg(quality_score)::numeric, 3) AS avg_quality,
       count(*) FILTER (WHERE quality_score > 0.7) AS high_quality
FROM articles
GROUP BY source_domain
ORDER BY avg_quality DESC;

-- 5. Recent failures (last 2 hours) with reason + status
SELECT created_at, source_domain, url, status_code,
       details->>'reason' AS reason
FROM crawl_events
WHERE event_type IN ('fetch_failed','blocked','gave_up','error')
  AND created_at > now() - interval '2 hours'
ORDER BY created_at DESC
LIMIT 50;

-- Bonus: live queue depth (what's left to crawl)
SELECT status, count(*) FROM crawl_queue GROUP BY status ORDER BY count DESC;
