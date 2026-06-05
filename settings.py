BOT_NAME = "cleancrawl"
SPIDER_MODULES = ["cleancrawl.spiders"]
NEWSPIDER_MODULE = "cleancrawl.spiders"

ROBOTSTXT_OBEY = True
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0
DOWNLOAD_DELAY = 1.0
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DEPTH_LIMIT = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DOWNLOADER_MIDDLEWARES = {
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": 550,
    "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": 100,
}

RETRY_TIMES = 2
RETRY_HTTP_CODES = [500, 502, 503, 504, 408]

# Don't follow redirects to login/paywall pages
REDIRECT_MAX_TIMES = 3

HTTPCACHE_ENABLED = False
LOG_LEVEL = "WARNING"

FEEDS = {
    "articles.jsonl": {
        "format": "jsonlines",
        "overwrite": True,
        "encoding": "utf8",
    }
}
