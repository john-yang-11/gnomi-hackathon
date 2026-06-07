# CleanCrawl — GNOMI Hackathon 2026

A respectful, queue-driven **article crawler** that collects clean, unique, high-quality content from news sites, blogs, and wiki-style pages — built for downstream use like search, summarization, and retrieval-augmented generation (RAG).

It discovers articles, classifies them, extracts clean content, scores quality, removes duplicates, and stores everything in a Postgres (Supabase) database — while respecting `robots.txt`, throttling per-domain, and detecting anti-bot pages.

---

## Features → Judging Criteria

| Criterion | How CleanCrawl meets it |
|---|---|
| **Crawl safety** | Obeys `robots.txt`, per-domain rate limiting, exponential-backoff retries, domain circuit breaker |
| **Anti-bot detection** | Detects HTTP 403/429/503, Cloudflare challenges, CAPTCHA pages, suspicious empty bodies — in multiple languages |
| **Content extraction** | Title, author, publish date, body, headings, canonical URL, language, summary (trafilatura + JSON-LD + OpenGraph) |
| **Duplicate detection** | URL normalization + canonical URL, exact SHA-256 hashing, MinHash LSH near-duplicate detection (survives across runs) |
| **Messy HTML handling** | trafilatura on broken/noisy HTML, plus BOM and Brotli-compression handling |
| **Page classification** | Separates real content (article/blog/wiki) from junk (tag/category/login/search/archive); labels `content_type` as `news`/`blog`/`wiki`/`article` |
| **Quality scoring** | 0–1 score with explainable reasons: length, metadata, headings, language, **freshness**, **uniqueness** |
| **Scalability** | Durable Postgres queue, retry system, per-domain limits, append-only event log, monitoring-friendly design |

---

## Architecture

```
Discovery (sitemap / RSS / robots.txt)
        │
        ▼
   crawl_queue ──► Claim batch ──► Throttle ──► Fetch ──► Anti-bot check
                                                              │
        ┌─────────────────────────────────────────────────────┘
        ▼
   Classify ──► Extract ──► Quality score ──► Dedup ──► Save (articles)
        │                                                   │
        └──────────── discover links, enqueue ◄─────────────┘
```

Every URL is a row in `crawl_queue` with a status and retry metadata, and every decision is logged to an append-only `crawl_events` table. This makes the crawler **resumable, retry-safe, and fully observable**.

### Project structure

```
cleancrawl/
├── cleancrawl/
│   ├── fetcher.py        # httpx fetching, robots.txt, rate limiting, anti-bot detection
│   ├── classifier.py     # URL + JSON-LD page classification (article vs junk; multilingual)
│   ├── extractor.py      # trafilatura extraction, quality scoring, content_type, language detection
│   ├── dedup.py          # URL canonicalization + SHA-256 + MinHash LSH near-duplicate detection
│   ├── stats.py          # crawl statistics tracker
│   ├── storage.py        # Supabase persistence via psycopg2 (queue, events, duplicates, domains)
│   └── runner.py         # queue-driven crawl loop (seed → claim → process → discover)
├── migrations/
│   ├── 002_crawl_infra.sql      # crawl_queue, crawl_events, duplicates, domain_state
│   └── monitoring_queries.sql   # dashboard queries
├── main.py               # CLI entry point
├── requirements.txt
└── .env                  # DATABASE_URL (not committed)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure the database

Create a `.env` file in the project root with your Supabase **Session pooler** connection string:

```
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-1-<region>.pooler.supabase.com:5432/postgres
```

> **Use the Session pooler** (port 5432), not the direct connection. The direct host is IPv6-only and unreliable on many networks; the pooler works over IPv4 and supports the server-side cursors this project uses.

### 3. Create the tables

The `articles` table plus the crawl infrastructure:

```bash
psql "$DATABASE_URL" -f migrations/002_crawl_infra.sql
```

(The `articles` table and `content_type` column are created/migrated automatically on first run if missing.)

---

## Usage

```bash
# Crawl one or more domains, stop after N articles
python main.py --domains techcrunch.com,arstechnica.com,bbc.com --rate 0.5 --limit 30

# Crawl everything reachable (no limit)
python main.py --domains npr.org,theguardian.com --rate 1.0

# Resume from the existing queue (no re-seeding)
python main.py --resume
```

### Command-line flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `--domains` | list | — | Comma-separated domains to crawl (seeds the queue) |
| `--limit` | int | `0` | Stop after N saved articles (`0` = no limit) |
| `--rate` | float | `1.5` | Seconds between requests **per domain** (lower = faster) |
| `--quality` | float | `0.25` | Minimum quality score (0–1) to keep an article |
| `--depth` | int | `5` | How many link-hops deep to follow from seed articles |
| `--resume` | flag | off | Skip seeding; crawl existing `pending` queue rows |
| `--scrapy` | flag | off | Use the legacy Scrapy engine (fallback) |

---

## Output

Articles are stored in the `articles` table:

```json
{
  "url": "https://techcrunch.com/2026/06/05/story",
  "canonical_url": null,
  "title": "Story Title",
  "author": "Jane Smith",
  "publish_date": "2026-06-05",
  "language": "en",
  "summary": "Short description…",
  "main_text": "Full clean article body…",
  "headings": ["Intro", "Details"],
  "source_domain": "techcrunch.com",
  "content_type": "news",
  "quality_score": 0.84,
  "quality_reasons": {
    "text_length": 0.25, "has_title": 0.10, "has_author": 0.10,
    "has_date": 0.05, "headings": 0.10, "language_detected": 0.10,
    "sentence_count": 0.10, "freshness": 0.10, "uniqueness": 0.07
  }
}
```

### Database tables

| Table | Purpose |
|---|---|
| `articles` | The clean, extracted, scored articles |
| `crawl_queue` | The frontier — one row per URL with status + retry metadata |
| `crawl_events` | Append-only audit log of every crawl decision |
| `duplicates` | Rejected duplicates, with the reason and similarity |
| `domain_state` | Per-domain throttling + health (circuit breaker) |

---

## Monitoring

```sql
-- Content-type breakdown
SELECT content_type, count(*) FROM articles GROUP BY content_type ORDER BY count DESC;

-- Language breakdown (multilingual)
SELECT language, count(*), round(avg(quality_score)::numeric, 2) AS avg_q
FROM articles GROUP BY language ORDER BY count DESC;

-- Crawl success rate
SELECT
  count(*) FILTER (WHERE event_type = 'saved')   AS saved,
  count(*) FILTER (WHERE event_type = 'blocked') AS blocked,
  count(*) FILTER (WHERE event_type = 'duplicate') AS duplicates
FROM crawl_events;

-- Live queue depth
SELECT status, count(*) FROM crawl_queue GROUP BY status ORDER BY count DESC;

-- Blocked pages by domain
SELECT source_domain, count(*) FROM crawl_events
WHERE event_type = 'blocked' GROUP BY source_domain ORDER BY count DESC;
```

More in `migrations/monitoring_queries.sql`.

---

## How it works

1. **Seed** — for each domain, read `robots.txt` + sitemaps + RSS feeds, enqueue up to 40 article URLs.
2. **Claim** — pull a batch of `pending` URLs that are due (FIFO with priority + backoff).
3. **Process** — throttle → fetch → anti-bot check → classify → extract → quality score → dedup → save.
4. **Discover** — enqueue article links found on each page (up to `--depth`).
5. **Repeat** — until the queue is empty or `--limit` is hit.

**Failure handling:** a failed fetch is retried up to **3 times** with exponential backoff (30s → 60s → 120s), then given up. If a domain blocks the crawler **4 times in a row**, the whole domain is backed off for 5 minutes (circuit breaker). Stalled `in_progress` rows are reclaimed after 10 minutes, and unfinished rows are released back to `pending` on shutdown — so nothing gets stranded and `--resume` always works.

---

## Deduplication (4 layers)

Checks run cheapest-first; the first hit wins, and every catch is logged to the `duplicates` table with its reason.

1. **URL** — aggressive normalization collapses the many shapes of one article: tracking params, `www`, `http`/`https`, AMP / mobile / print path segments, trailing slashes, default ports.
2. **Exact content** — SHA-256 of the clean article text.
3. **Near-duplicate** — MinHash LSH at 80% Jaccard similarity, with **CJK-aware shingling** (character n-grams for Chinese/Japanese/Korean, word n-grams for Latin) so syndicated/reposted copies are caught across languages.
4. **Title + date** — same normalized title published on the same date, catching syndicated copies whose body was reformatted enough to slip past the near-dup check.

The fingerprint index is rehydrated from the database at startup (`warm_dedup`), so dedup survives across runs and `--resume`.

---

## Freshness-aware recrawl (self-refreshing)

The system is built to re-crawl on its own and keep content fresh — split into two halves:

- **Scheduling (live, in the database):** a `pg_cron` job (`migrations/003_recrawl_pgcron.sql`) runs hourly inside Supabase and re-enqueues `done` articles whose freshness window has elapsed, by content type — **news 6h, blog 48h, wiki 7d, article 24h**. No server, no app code.
- **Execution:** running the crawler (`python main.py --resume`) drains the re-enqueued URLs. Dedup handles the rest — unchanged pages hit the `duplicate` path, changed pages produce updated records.

To make the loop **fully hands-off**, the included GitHub Actions workflow (`.github/workflows/recrawl.yml`) can run the crawler on a schedule (manual-trigger by default; uncomment the `schedule:` block to auto-run hourly). Together: the database decides *what* to recrawl, the workflow does it, with zero human involvement.

A complementary signal: the **freshness sub-score** (part of quality scoring) applies an age-decay curve, so stale content naturally ranks lower over time even between recrawls.

---

## Multilingual support

Classification, anti-bot detection, language detection, and quality scoring all work across languages — European (EN/ES/FR/DE/PT/IT) and CJK/Arabic scripts. Percent-encoded non-Latin URLs are decoded, CJK quality thresholds are scaled (denser scripts), and block-page phrases are matched in multiple languages.

---

## Tech stack

Python · Scrapy (legacy engine) · trafilatura · httpx · datasketch (MinHash) · langdetect · psycopg2 · Supabase (Postgres)
