"""
Queue-driven crawl runner. Replaces Scrapy as the crawl driver.

Flow:
  seed_queue(domains)
    → claim_pending() from crawl_queue
    → fetch (fetcher.py)
    → classify (classifier.py)
    → extract (extractor.py)
    → dedup (dedup.py)
    → save article + log events + update queue status
    → discover + enqueue new links
    → repeat until queue empty or limit hit
"""
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

from cleancrawl.classifier import classify_url, classify_page
from cleancrawl.dedup import DedupStore
from cleancrawl.extractor import extract
from cleancrawl.fetcher import Fetcher, HEADERS
from cleancrawl.stats import CrawlStats
from cleancrawl import storage as db

# Discovery paths tried per domain when seeding.
# ORDER MATTERS: RSS/Atom feeds and news-sitemaps first — they list CURRENT
# articles directly. Giant generic sitemap.xml indexes go LAST so they don't
# burn the sub-sitemap budget before we reach the useful feeds.
DISCOVERY_PATHS = [
    # current-article feeds (best signal)
    "/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/feed.xml",
    "/feeds/posts/default", "/feeds/all.atom.xml", "/index.xml",
    "/?feed=rss2", "/feed/rss",
    # news sitemaps (current articles, small)
    "/news-sitemap.xml", "/sitemap-news.xml",
    # generic indexes (often huge archives) — last resort
    "/sitemap_index.xml", "/sitemap.xml",
]


def _sitemap_priority(url: str) -> int:
    """Lower = process earlier. Prefer feeds/news sitemaps over giant indexes."""
    u = url.lower()
    if any(k in u for k in ("/feed", "/rss", "atom", "rss2")):
        return 0
    if "news" in u and "sitemap" in u:
        return 1
    if "sitemap_index" in u or u.rstrip("/").endswith("/sitemap.xml"):
        return 3   # generic index last
    return 2


def _is_sitemap_url(url: str) -> bool:
    """
    True if the URL is itself a sitemap (index or sub-sitemap), even when it
    carries query params (e.g. propublica's sitemap.xml?yyyy=2026&mm=06).
    Without this, such URLs get misclassified as articles and pollute the queue.
    """
    path = urlparse(url).path.lower()
    return "sitemap" in path and (path.endswith(".xml") or "sitemap" in path)


MAX_PATH_DEPTH = 8
MAX_QUERY_PARAMS = 3


def _is_trap(url: str) -> bool:
    p = urlparse(url)
    return p.path.count("/") > MAX_PATH_DEPTH or p.query.count("&") >= MAX_QUERY_PARAMS


def _retry_backoff(attempts: int) -> int:
    """Exponential backoff in seconds: 30s, 60s, 120s."""
    return 30 * (2 ** attempts)


def _clean_xml(text: str) -> str:
    """Strip BOM and leading whitespace so the XML parser doesn't choke.
    Many feeds (TechCrunch, ProPublica, The Verge) start with a \\ufeff BOM."""
    return text.lstrip("﻿￾ \t\r\n")


def _extract_urls_from_xml(text: str) -> list[str]:
    """Parse sitemap or RSS/Atom and return all article-candidate URLs."""
    try:
        root = ET.fromstring(_clean_xml(text).encode())
    except ET.ParseError:
        return []

    urls = []

    def tag(el):
        return el.tag.split("}")[-1] if "}" in el.tag else el.tag

    for el in root.iter():
        t = tag(el)
        if t == "loc":
            loc = (el.text or "").strip()
            if loc:
                urls.append(loc)
        elif t == "link" and el.text and el.text.strip().startswith("http"):
            urls.append(el.text.strip())
        elif t == "link":
            href = el.get("href", "").strip()
            if href.startswith("http") and el.get("rel", "alternate") == "alternate":
                urls.append(href)
        elif t == "guid":
            g = (el.text or "").strip()
            if g.startswith("http"):
                urls.append(g)
    return urls


def _extract_links_from_html(html: str, base_url: str, base_domain: str) -> list[str]:
    """Pull same-domain <a href> links from an article page for link-following."""
    from lxml import etree
    try:
        tree = etree.fromstring(html.encode(), etree.HTMLParser())
    except Exception:
        return []
    links = []
    for href in tree.xpath("//a/@href"):
        href = href.strip()
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        # Make absolute
        if href.startswith("/"):
            p = urlparse(base_url)
            href = f"{p.scheme}://{p.netloc}{href}"
        if urlparse(href).netloc == base_domain:
            links.append(href)
    return links


class Runner:
    def __init__(self, rate_limit: float = 1.5, limit: int = 0,
                 min_quality: float = 0.25, depth_limit: int = 5,
                 translate: bool = False):
        # Disable the Fetcher's internal throttle — the runner already
        # throttles per-domain via db.due_domain()/touch_domain(). Having both
        # made every fetch sleep twice (~2x slower).
        self.fetcher = Fetcher(rate_limit=0.0)
        self.rate = rate_limit       # used as the per-domain crawl_delay
        self.dedup = DedupStore()
        self.stats = CrawlStats()
        self.limit = limit          # 0 = no limit
        self.min_quality = min_quality
        self.depth_limit = depth_limit
        self.translate = translate   # --translate: add English fields for non-EN articles

    # ─────────────────────────────────────────────────────────
    # Seeding
    # ─────────────────────────────────────────────────────────
    def seed_queue(self, domains: list[str], max_per_domain: int = 40,
                   max_sitemaps_per_domain: int = 3) -> int:
        """
        Probe discovery paths for each domain and enqueue article URLs.

        Bounded so seeding finishes fast even on huge news sitemaps:
          - stops after `max_per_domain` URLs are enqueued for a domain
          - recurses into at most `max_sitemaps_per_domain` sub-sitemaps
        Without these caps a single site (e.g. Al Jazeera) can enqueue
        thousands of URLs and the crawl loop never gets to start.
        """
        total = 0
        # Short timeout during seeding so a slow/hanging sitemap probe
        # doesn't stall the whole seed phase.
        client = httpx.Client(headers=HEADERS, timeout=8,
                              follow_redirects=True, verify=False)
        for domain in domains:
            base = f"https://{domain}"
            domain_count = 0
            sitemaps_followed = 0

            # Discovery sources = sitemaps declared in robots.txt (authoritative,
            # handles non-standard paths/subdomains) + guessed fallback paths.
            sources = self._robots_sitemaps(client, base) + [base + p for p in DISCOVERY_PATHS]
            # Dedupe, then sort so feeds/news-sitemaps are tried before giant
            # generic sitemap indexes.
            stack = sorted(dict.fromkeys(sources), key=_sitemap_priority)
            visited: set[str] = set()

            while stack and domain_count < max_per_domain:
                src = stack.pop(0)
                if src in visited:
                    continue
                visited.add(src)
                try:
                    resp = client.get(src)
                    if resp.status_code != 200:
                        continue
                    text = _clean_xml(resp.text)   # strips BOM + whitespace
                    if not (text.startswith("<?xml") or text.startswith("<rss")
                            or text.startswith("<feed")):
                        continue

                    for found_url in _extract_urls_from_xml(text):
                        if domain_count >= max_per_domain:
                            break
                        # Nested sitemap (handles query-string sitemaps too) →
                        # push onto the stack to expand later (bounded).
                        if _is_sitemap_url(found_url):
                            if sitemaps_followed < max_sitemaps_per_domain and found_url not in visited:
                                sitemaps_followed += 1
                                # keep priority order: news/content sitemaps first
                                stack.append(found_url)
                                stack.sort(key=_sitemap_priority)
                        else:
                            added = self._maybe_enqueue(found_url, domain, depth=0, from_url=src)
                            domain_count += added
                            total += added
                except Exception:
                    continue

            print(f"[Seed]   {domain}: {domain_count} URLs")
        client.close()
        print(f"[Seed] Enqueued {total} URLs from {len(domains)} domain(s)")

    def _robots_sitemaps(self, client, base: str) -> list[str]:
        """Read robots.txt and return any declared Sitemap: URLs."""
        try:
            r = client.get(base + "/robots.txt")
            if r.status_code != 200:
                return []
            sitemaps = []
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    if url.startswith("http"):
                        sitemaps.append(url)
            return sitemaps
        except Exception:
            return []

    def _maybe_enqueue(self, url: str, domain: str, depth: int,
                       from_url: str | None = None) -> int:
        """Classify URL and enqueue if article-like and not a trap."""
        if _is_trap(url):
            return 0
        # Never enqueue a sitemap as if it were an article (query-string
        # sitemaps like sitemap.xml?yyyy=2026 otherwise slip through).
        if _is_sitemap_url(url):
            return 0
        page_type, _ = classify_url(url)
        if page_type != "article":
            return 0
        dup, _ = self.dedup.is_duplicate_url(url)
        if dup:
            return 0
        if db.enqueue_url(url, domain, depth=depth, discovered_from=from_url):
            db.log_event("enqueued", url=url, source_domain=domain,
                         details={"depth": depth})
            return 1
        return 0

    # ─────────────────────────────────────────────────────────
    # Dedup rehydration — load already-saved articles so content
    # dedup survives across runs (Fix #2)
    # ─────────────────────────────────────────────────────────
    def warm_dedup(self) -> int:
        """
        Rebuild the in-memory DedupStore (URLs + content hashes + MinHash LSH)
        from articles already in the database, so a --resume run won't re-save
        syndicated content it has seen before.
        """
        loaded = 0
        for url, text in db.iter_articles_for_dedup():
            self.dedup.register_url(url)
            if text:
                self.dedup.register_content(text, doc_id=url)
            loaded += 1
        if loaded:
            print(f"[Dedup] Rehydrated {loaded} existing articles into dedup store")
        return loaded

    # ─────────────────────────────────────────────────────────
    # Main crawl loop
    # ─────────────────────────────────────────────────────────
    def run(self, batch_size: int = 10, stale_timeout_minutes: int = 10) -> None:
        extracted = 0
        idle_rounds = 0

        # Fix #2: warm the dedup store from existing DB articles before crawling
        self.warm_dedup()

        # Fix #1: recover any rows stranded as in_progress by a prior crash
        reclaimed = db.reclaim_stale(timeout_minutes=stale_timeout_minutes)
        if reclaimed:
            print(f"[Runner] Reclaimed {reclaimed} stale in_progress URL(s)")

        print(f"[Runner] Starting crawl loop (limit={self.limit or 'none'})")

        try:
            while True:
                # Stop if we've hit the article limit
                if self.limit and extracted >= self.limit:
                    print(f"[Runner] Limit of {self.limit} articles reached")
                    break

                # Periodically reclaim stale rows during long runs too
                db.reclaim_stale(timeout_minutes=stale_timeout_minutes)

                batch = db.claim_pending(limit=batch_size)

                if not batch:
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        print("[Runner] Queue empty — crawl complete")
                        break
                    time.sleep(2)
                    continue

                idle_rounds = 0

                for item in batch:
                    if self.limit and extracted >= self.limit:
                        break
                    result = self._process(item)
                    if result == "saved":
                        extracted += 1
        finally:
            # Fix #1: never strand rows we claimed but didn't finish
            # (e.g. when --limit is hit mid-batch, or on Ctrl-C)
            released = db.release_in_progress()
            if released:
                print(f"[Runner] Released {released} unfinished in_progress URL(s) back to pending")

        self.stats.print_summary()
        # Persist stats to disk (main.py advertises this file)
        try:
            import json
            with open("crawl_stats.json", "w", encoding="utf-8") as f:
                json.dump(self.stats.summary(), f, indent=2)
        except Exception as e:
            print(f"[Runner] Could not write crawl_stats.json: {e}")

    def _process(self, item: dict) -> str:
        """
        Fetch and process one queued URL. Returns one of:
        saved | duplicate | low_quality | blocked | skipped | error
        """
        url = item["url"]
        domain = item["source_domain"]
        queue_id = item["id"]
        depth = item["depth"]
        attempts = item["attempts"]

        # ── Per-domain throttle + circuit breaker ────────────
        ok, wait = db.due_domain(domain)
        if not ok:
            if wait > 10.0:
                # Domain is circuit-broken (backed off). Don't fetch — defer this
                # URL by pushing next_attempt_at past the backoff window so it
                # leaves the claimable set (prevents the claim→requeue livelock),
                # and move on to other domains.
                db.mark_queue_status(url, "pending", defer_seconds=int(wait) + 1)
                db.log_event("retry_scheduled", url=url, source_domain=domain,
                             queue_id=queue_id,
                             details={"reason": "domain_backoff",
                                      "wait_seconds": round(wait, 1)})
                return "skipped"
            # Normal crawl-delay throttle: short wait, then proceed.
            time.sleep(wait)

        # ── robots.txt check ────────────────────────────────
        if not self.fetcher.can_fetch(url):
            db.mark_queue_status(url, "skipped", error="robots_txt")
            db.log_event("classified_skip", url=url, source_domain=domain,
                         queue_id=queue_id, details={"reason": "robots_txt"})
            self.stats.record_skip()
            return "skipped"

        # ── Fetch ────────────────────────────────────────────
        db.log_event("fetch_started", url=url, source_domain=domain, queue_id=queue_id)
        result = self.fetcher.fetch(url)
        db.touch_domain(domain, fetched=True, blocked=result.blocked,
                        crawl_delay=self.rate)
        self.stats.record_fetch(domain, blocked=result.blocked, reason=result.block_reason)

        if result.blocked or result.html is None:
            backoff = _retry_backoff(attempts)
            db.mark_queue_status(url, "failed", error=result.block_reason,
                                 retry_backoff_seconds=backoff)
            db.log_event("blocked" if result.blocked else "fetch_failed",
                         url=url, source_domain=domain, queue_id=queue_id,
                         status_code=result.status,
                         details={"block_reason": result.block_reason,
                                  "attempt": attempts + 1, "next_backoff": backoff})
            return "blocked"

        # ── Content-level classification ─────────────────────
        page_type, reason = classify_page(result.html, url)
        if page_type != "article":
            db.mark_queue_status(url, "skipped", error=f"classifier:{reason}")
            db.log_event("classified_skip", url=url, source_domain=domain,
                         queue_id=queue_id, details={"reason": reason})
            self.stats.record_skip()
            return "skipped"

        # ── Extract ──────────────────────────────────────────
        article = extract(result.html, url)
        if article is None or article.quality_score < self.min_quality:
            db.mark_queue_status(url, "skipped", error="low_quality")
            db.log_event("low_quality", url=url, source_domain=domain,
                         queue_id=queue_id,
                         details={"quality_score": article.quality_score if article else 0})
            self.stats.record_low_quality()
            return "low_quality"

        # ── Dedup ────────────────────────────────────────────
        check_url = article.canonical_url or url
        is_dup, dup_reason = self.dedup.check_and_register(check_url, article.main_text)
        if is_dup:
            dup_type = (
                "url" if "url" in dup_reason
                else "exact_content" if "exact_hash" in dup_reason
                else "near_content"
            )
            db.save_duplicate(
                url=url, dup_type=dup_type, canonical_url=article.canonical_url,
                source_domain=domain,
                duplicate_of=dup_reason.split(":")[-1] if ":" in dup_reason else None,
                details={"reason": dup_reason},
            )
            db.mark_queue_status(url, "done", error=f"duplicate:{dup_reason}")
            db.log_event("duplicate", url=url, source_domain=domain, queue_id=queue_id,
                         details={"dup_type": dup_type, "reason": dup_reason})
            self.stats.record_duplicate(dup_reason)
            return "duplicate"

        # ── Save ─────────────────────────────────────────────
        record = {
            "url": url,
            "canonical_url": article.canonical_url,
            "title": article.title,
            "author": article.author,
            "publish_date": article.publish_date,
            "language": article.language,
            "summary": article.summary,
            "main_text": article.main_text,
            "headings": article.headings,
            "source_domain": article.source_domain,
            "quality_score": article.quality_score,
            "content_type": article.content_type,
            "quality_reasons": article.quality_reasons,
        }

        # ── Optional English translation (--translate) ───────
        if self.translate:
            from cleancrawl.translate import translate_article
            translate_article(record)   # adds *_en fields for non-EN articles

        article_id = db.save_article(record)
        if article_id:
            db.mark_queue_status(url, "done", article_id=article_id)
            db.log_event("saved", url=url, source_domain=domain,
                         queue_id=queue_id, article_id=article_id,
                         details={"quality_score": article.quality_score,
                                  "language": article.language})
            self.stats.record_extract(article.quality_score)

            # ── Discover new links (if not too deep) ─────────
            if depth < self.depth_limit:
                for link in _extract_links_from_html(result.html, url, domain):
                    self._maybe_enqueue(link, domain, depth=depth + 1, from_url=url)

            return "saved"
        else:
            backoff = _retry_backoff(attempts)
            db.mark_queue_status(url, "failed", error="save_failed",
                                 retry_backoff_seconds=backoff)
            db.log_event("error", url=url, source_domain=domain, queue_id=queue_id,
                         details={"error": "save_failed"})
            return "error"

    def close(self) -> None:
        self.fetcher.close()
