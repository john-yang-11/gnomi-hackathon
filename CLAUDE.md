# CleanCrawl — GNOMI Hackathon 2026

Article crawler that collects clean, unique, high-quality content from news sites, blogs, and wiki-style pages. Built for RAG/search/summarization downstream use.

## Project structure

```
cleancrawl/
├── cleancrawl/
│   ├── fetcher.py          # httpx fetching, robots.txt, rate limiting, multilingual anti-bot detection
│   ├── classifier.py       # multilingual URL + JSON-LD/og:type page classification (article vs junk)
│   ├── extractor.py        # trafilatura extraction + language-aware quality scoring + language detection
│   ├── dedup.py            # URL canonicalization + SHA256 + MinHash LSH near-duplicate detection
│   ├── stats.py            # crawl statistics tracker
│   ├── storage.py          # Supabase persistence via direct psycopg2 connection
│   └── spiders/
│       └── article_spider.py  # Scrapy 2.16 spider (uses async def start(), not start_requests)
├── settings.py             # Scrapy settings
├── scrapy.cfg
├── main.py                 # CLI entry point
├── requirements.txt
└── CLAUDE.md               # this file
```

## How to run

```bash
pip install -r requirements.txt

# Crawl one or more domains, stop after N articles
python main.py --domains blog.python.org --limit 50
python main.py --domains bbc.com,techcrunch.com,en.wikipedia.org --limit 100

# Output destinations
articles.jsonl      # one article per line (JSON)
crawl_stats.json    # crawl summary stats
Supabase            # articles upserted to the `articles` table (see storage.py)
```

## Architecture

```
Discovery (sitemap/RSS/feed) → Classifier → Fetcher → Extractor → Dedup → Stats → Output (JSONL + Supabase)
```

Each domain is probed with 14 common discovery paths (sitemap.xml, /feed, /rss, /feeds/posts/default, etc). The spider routes based on actual XML content (not Content-Type headers, which are unreliable).

## Key decisions / gotchas

- **Scrapy 2.16**: uses `async def start(self)` NOT `def start_requests(self)` — old name is silently ignored, spider closes with 0 results
- **trafilatura API**: uses `with_metadata=True` NOT `include_metadata=True` (changed in newer versions)
- **XML parsing in parse_discovery**: uses `xml.etree.ElementTree` directly, NOT Scrapy's XPath selector — Scrapy parses RSS `<link>` as HTML void elements so XPath returns nothing
- **Content-Type unreliable**: python blog returns `application/octet-stream` for its RSS feed; routing is based on whether text starts with `<?xml`, `<rss`, or `<feed`

## Output schema (articles.jsonl)

```json
{
  "url": "https://...",
  "canonical_url": "https://...",
  "title": "Article Title",
  "author": "Jane Smith",
  "publish_date": "2026-05-01",
  "language": "en",
  "summary": "Short description...",
  "main_text": "Full article body...",
  "headings": ["Heading 1", "Heading 2"],
  "source_domain": "example.com",
  "quality_score": 0.80,
  "quality_reasons": {
    "text_length": 0.35,
    "has_title": 0.15,
    "has_author": 0.10,
    "has_date": 0.10,
    "headings": 0.10,
    "language_detected": 0.10,
    "sentence_count": 0.10
  }
}
```

## Multilingual support (added)

The crawler handles non-English sites:
- **Classifier** (`classifier.py`): article/junk URL patterns in EN, ES, FR, DE, PT, IT + romanized CJK/Arabic. Decodes percent-encoded paths (e.g. Japanese `/記事/`) and detects non-Latin script slugs via `NON_LATIN_SCRIPT` regex.
- **Anti-bot** (`fetcher.py`): `BLOCK_PHRASES` and `CHALLENGE_PHRASES` cover block/captcha pages in major languages. Sends a broad `Accept-Language` header.
- **Quality scoring** (`extractor.py`): `_is_cjk()` detection scales length/sentence thresholds down for CJK (info-dense). `_SENTENCE_SPLIT` handles CJK/Arabic/Devanagari punctuation.
- **Language detection** (`extractor.py` `_detect_language`): trafilatura → JSON-LD inLanguage → `<html lang>` → langdetect fallback (script-aware min length).

## Storage / Supabase (added, working)

- `storage.py` uses a **direct psycopg2 Postgres connection** (NOT the REST API/supabase-py client — PostgREST schema cache kept failing with PGRST205 on the fresh project). Connection string is the Supabase DB URI.
- `save_article()` upserts on `url` conflict, refreshing ALL fields (early version only updated some, so re-crawls kept stale NULLs — fixed).
- `ftfy.fix_text()` applied to title/summary/main_text to fix Windows-1252 smart quotes.
- The MCP `apply_migration` created the table in a DIFFERENT db than the connection string points to — the real table was created directly via psycopg2. Verify with a direct connection, not the MCP tools.

## Known issues / TODO

- Author still often null on some sites — JSON-LD author extraction added but not all sites expose it
- Cross-domain dedup only works within a single crawl run (in-memory MinHash LSH) — syndicated content across separate runs not caught
- No recrawl scheduler for updated articles
- Not yet stress-tested against hard targets (BBC, CNN, Cloudflare-protected sites)

## Supabase schema (live)

Table created directly via psycopg2 (NOT via MCP — see gotcha above). Schema:

```sql
create table articles (
  id uuid default gen_random_uuid() primary key,
  url text unique not null,
  canonical_url text,
  title text,
  author text,
  publish_date text,
  language text,
  summary text,
  main_text text,
  headings jsonb,
  source_domain text,
  quality_score float,
  quality_reasons jsonb,
  crawled_at timestamptz default now()
);
```

## Test results (confirmed working)

- blog.python.org: ~17-22 articles, avg quality 0.836, 0 blocked, language tagged `en`, rows landing in Supabase
- Classifier: 8/8 English + 14/14 multilingual URL pattern tests pass (ES/FR/DE/PT/IT + CJK/Arabic, incl. percent-encoded paths)
- Language detection: verified on en/es/fr/ja/zh-cn/ko/ar
- Dedup: exact hash + near-duplicate (MinHash LSH) both confirmed working
- Quality score rose 0.74 → 0.836 once language detection started scoring (+0.10/article)

## Hackathon judging criteria

1. Crawl safety — robots.txt obeyed, autothrottle, per-domain rate limiting
2. Anti-bot detection — HTTP 403/429/503, Cloudflare challenge, CAPTCHA, empty body
3. Content extraction — title, author, date, body, headings, canonical URL, quality score
4. Duplicate detection — URL normalization (strips utm_*, fbclid), SHA256 exact, MinHash near-dupe
5. Messy HTML handling — trafilatura handles broken tags, nested divs, missing metadata
