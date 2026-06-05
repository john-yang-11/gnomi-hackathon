"""
Main Scrapy spider for CleanCrawl.
Discovers articles via sitemaps, RSS/Atom feeds, and link following.
Applies classifier → extractor → dedup pipeline.
"""
import json
from urllib.parse import urlparse

import scrapy
from cleancrawl.storage import save_article
from cleancrawl.fetcher import BLOCK_PHRASES, CHALLENGE_PHRASES

from cleancrawl.classifier import classify_url, classify_page
from cleancrawl.extractor import extract
from cleancrawl.dedup import DedupStore
from cleancrawl.stats import CrawlStats

_dedup = DedupStore()
_stats = CrawlStats()

# Common feed/sitemap discovery paths to try per domain
DISCOVERY_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-news.xml",
    "/news-sitemap.xml",
    "/feed",
    "/feed/",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/feeds/posts/default",   # Blogger
    "/feeds/all.atom.xml",    # Ghost
    "/index.xml",             # Hugo
    "/?feed=rss2",            # WordPress
    "/feed/rss",
]

MAX_QUERY_PARAMS = 3
MAX_PATH_DEPTH = 8


def _is_crawler_trap(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.query.count("&") >= MAX_QUERY_PARAMS:
        return True
    if parsed.path.count("/") > MAX_PATH_DEPTH:
        return True
    return False


class ArticleSpider(scrapy.Spider):
    name = "articles"

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "DEPTH_LIMIT": 5,
        "LOG_LEVEL": "WARNING",
        "FEEDS": {
            "articles.jsonl": {
                "format": "jsonlines",
                "overwrite": True,
                "encoding": "utf8",
            }
        },
    }

    def __init__(self, start_domains: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._domains = [
            d.strip().lstrip("https://").lstrip("http://")
            for d in start_domains.split(",")
            if d.strip()
        ]

    async def start(self):
        for domain in self._domains:
            base = f"https://{domain}"
            for path in DISCOVERY_PATHS:
                url = base + path
                yield scrapy.Request(
                    url,
                    callback=self.parse_discovery,
                    errback=self.handle_error,
                    dont_filter=True,
                    meta={"handle_httpstatus_list": [404, 403, 410]},
                )

    def parse_discovery(self, response):
        """Route sitemaps, RSS/Atom feeds, or HTML homepages by content, not headers."""
        if response.status in (404, 403, 410):
            return

        text = response.text.lstrip()

        # Detect format from actual content, not Content-Type (servers lie)
        is_xml = text.startswith("<?xml") or text.startswith("<rss") or text.startswith("<feed")

        if is_xml:
            urls = list(self._extract_xml_urls(text, response.url))
            for loc, is_sitemap_index in urls:
                if is_sitemap_index:
                    yield scrapy.Request(loc, callback=self.parse_discovery,
                                         errback=self.handle_error)
                else:
                    yield from self._maybe_crawl(loc)
            return

        # HTML fallback: follow same-domain links
        for href in response.css("a::attr(href)").getall():
            abs_url = response.urljoin(href)
            if urlparse(abs_url).netloc == urlparse(response.url).netloc:
                yield from self._maybe_crawl(abs_url)

    def _extract_xml_urls(self, text: str, base_url: str):
        """
        Extract URLs from sitemap or RSS/Atom feed using ElementTree.
        Yields (url, is_sitemap_index) tuples.
        """
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(text.encode())
        except ET.ParseError:
            return

        # Strip namespaces for easier matching
        def tag(el):
            return el.tag.split("}")[-1] if "}" in el.tag else el.tag

        # Sitemap: look for <loc> tags
        for el in root.iter():
            if tag(el) == "loc":
                loc = (el.text or "").strip()
                if not loc:
                    continue
                is_index = "sitemap" in loc.lower() and loc.endswith(".xml")
                yield loc, is_index
            # RSS <link> element (text content)
            elif tag(el) == "link" and el.text and el.text.strip().startswith("http"):
                yield el.text.strip(), False
            # Atom <link href="...">
            elif tag(el) == "link":
                href = el.get("href", "").strip()
                rel = el.get("rel", "alternate")
                if href.startswith("http") and rel in ("alternate", ""):
                    yield href, False
            # RSS <guid> as fallback URL
            elif tag(el) == "guid":
                guid = (el.text or "").strip()
                if guid.startswith("http"):
                    yield guid, False

    def _maybe_crawl(self, url: str):
        """Yield a request if the URL looks like an article and isn't a duplicate."""
        if not url or not url.startswith("http"):
            return
        if _is_crawler_trap(url):
            return
        page_type, _ = classify_url(url)
        if page_type != "article":
            return
        dup, _ = _dedup.is_duplicate_url(url)
        if dup:
            return
        yield scrapy.Request(url, callback=self.parse_article,
                              errback=self.handle_error)

    def parse_article(self, response):
        url = response.url
        domain = urlparse(url).netloc

        blocked, reason = self._detect_block(response)
        _stats.record_fetch(domain, blocked=blocked, reason=reason)

        if blocked:
            self.logger.warning(f"BLOCKED [{reason}]: {url}")
            return

        # Content-level classification (JSON-LD / og:type check)
        page_type, reason = classify_page(response.text, url)
        if page_type != "article":
            _stats.record_skip()
            return

        article = extract(response.text, url)
        if article is None:
            _stats.record_low_quality()
            return

        if article.quality_score < 0.25:
            _stats.record_low_quality()
            return

        check_url = article.canonical_url or url
        is_dup, dup_reason = _dedup.check_and_register(check_url, article.main_text)
        if is_dup:
            _stats.record_duplicate(dup_reason)
            return

        _stats.record_extract(article.quality_score)

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
            "quality_reasons": article.quality_reasons,
        }
        save_article(record)
        yield record

        # Follow links to more articles (depth-limited by Scrapy)
        for href in response.css("a::attr(href)").getall():
            abs_url = response.urljoin(href)
            if not _is_crawler_trap(abs_url):
                yield from self._maybe_crawl(abs_url)

    def _detect_block(self, response) -> tuple[bool, str | None]:
        if response.status in (403, 429, 503):
            return True, f"http_{response.status}"
        html = response.text.lower()
        headers = {k.lower(): v for k, v in response.headers.items()}
        if "cf-ray" in headers and any(p in html for p in CHALLENGE_PHRASES):
            return True, "cloudflare_challenge"
        if "recaptcha" in html or "hcaptcha" in html:
            return True, "captcha"
        if any(p in html for p in BLOCK_PHRASES):
            return True, "access_denied"
        if len(response.text.strip()) < 300 and response.status == 200:
            return True, "suspicious_empty"
        return False, None

    def handle_error(self, failure):
        _stats.record_fetch(
            domain="unknown",
            blocked=False,
            reason=failure.type.__name__,
        )

    def closed(self, reason):
        _stats.print_summary()
        with open("crawl_stats.json", "w") as f:
            json.dump(_stats.summary(), f, indent=2)
