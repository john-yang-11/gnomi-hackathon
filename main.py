"""
CleanCrawl CLI — queue-driven article crawler.

Usage:
    python main.py --domains bbc.com,blog.python.org --limit 50
    python main.py --domains elpais.com --limit 20 --rate 2.0
    python main.py --resume          # pick up from existing queue (no seeding)
    python main.py --scrapy --domains blog.python.org --limit 10  # fallback
"""
import argparse
import os
os.environ.setdefault("SCRAPY_SETTINGS_MODULE", "settings")

from cleancrawl.runner import Runner


def main():
    parser = argparse.ArgumentParser(description="CleanCrawl — GNOMI Hackathon 2026")
    parser.add_argument("--domains", default="",
                        help="Comma-separated domains to seed (e.g. bbc.com,elpais.com)")
    parser.add_argument("--limit",  type=int, default=0,
                        help="Stop after N articles (0 = no limit)")
    parser.add_argument("--rate",   type=float, default=1.5,
                        help="Seconds between requests per domain (default 1.5)")
    parser.add_argument("--quality", type=float, default=0.25,
                        help="Minimum quality score to keep (default 0.25)")
    parser.add_argument("--depth",  type=int, default=5,
                        help="Max link-follow depth (default 5)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip seeding — resume from existing crawl_queue")
    parser.add_argument("--scrapy", action="store_true",
                        help="Use legacy Scrapy spider instead of queue runner")
    args = parser.parse_args()

    # ── Legacy Scrapy fallback ───────────────────────────────
    if args.scrapy:
        from scrapy.crawler import CrawlerProcess
        from scrapy.utils.project import get_project_settings
        from cleancrawl.spiders.article_spider import ArticleSpider
        settings = get_project_settings()
        if args.limit:
            settings.set("CLOSESPIDER_ITEMCOUNT", args.limit)
        process = CrawlerProcess(settings)
        process.crawl(ArticleSpider, start_domains=args.domains)
        process.start()
        return

    # ── Queue-driven runner ──────────────────────────────────
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    runner = Runner(
        rate_limit=args.rate,
        limit=args.limit,
        min_quality=args.quality,
        depth_limit=args.depth,
    )

    try:
        if not args.resume:
            if not domains:
                print("Error: --domains required unless --resume is set")
                return
            runner.seed_queue(domains)
        else:
            print("[Runner] Resuming from existing crawl_queue (skipping seed)")

        runner.run()
    finally:
        runner.close()

    print("\nDone. Articles in Supabase, stats in crawl_stats.json")


if __name__ == "__main__":
    main()
