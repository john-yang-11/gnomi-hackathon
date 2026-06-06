"""
Respectful fetcher: robots.txt, per-domain rate limiting, anti-bot detection.
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    # Accept many languages so non-English sites serve full content, not a redirect
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8,fr;q=0.8,de;q=0.7,pt;q=0.7,it;q=0.7,ja;q=0.6,zh;q=0.6,ar;q=0.6",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

# Block / access-denied phrases across major languages.
# Used to detect anti-bot pages served in the site's local language.
BLOCK_PHRASES = (
    # English
    "access denied", "you have been blocked", "are you a robot",
    "verify you are human", "request blocked",
    # Spanish
    "acceso denegado", "has sido bloqueado", "verifica que eres humano",
    # French
    "accès refusé", "acces refuse", "vous avez été bloqué", "vérifiez que vous êtes humain",
    # German
    "zugriff verweigert", "sie wurden blockiert", "bestätigen sie, dass sie ein mensch",
    # Portuguese
    "acesso negado", "você foi bloqueado", "verifique se você é humano",
    # Italian
    "accesso negato", "sei stato bloccato", "verifica di essere umano",
    # CJK
    "访问被拒绝", "アクセスが拒否されました", "접근이 거부되었습니다",
    # Arabic
    "تم رفض الوصول",
)

# "Checking your browser" challenge phrases across languages (Cloudflare etc.)
CHALLENGE_PHRASES = (
    "just a moment", "checking your browser", "enable javascript",
    "un momento", "vérification de votre navigateur", "ihr browser wird überprüft",
    "verificando seu navegador", "controllo del browser",
)

# High-confidence bot-WALL phrases (DataDome/PerimeterX/rate-limit pages).
# These NEVER appear in a real article, so they trigger a block regardless of
# page length — unlike captcha *widgets*, which can be embedded in real pages.
BOT_WALL_PHRASES = (
    # English
    "your traffic has been identified as automated",
    "traffic has been identified as automated",
    "unusual traffic", "automated traffic", "detected unusual activity",
    "suspicious activity has been detected", "bot detected",
    "to continue, please verify", "please verify you are a human",
    # French
    "votre trafic a été identifié comme automatisé",
    "trafic a été identifié comme automatisé", "trafic automatisé",
    "activité suspecte",
    # Spanish
    "tráfico automatizado", "actividad inusual", "actividad sospechosa",
    # German
    "automatisierter datenverkehr", "ungewöhnlicher datenverkehr",
    # Portuguese
    "tráfego automatizado", "atividade incomum",
    # Italian
    "traffico automatizzato", "attività insolita",
)


@dataclass
class FetchResult:
    url: str
    final_url: str
    html: str | None
    status: int
    blocked: bool
    block_reason: str | None
    elapsed: float
    headers: dict = field(default_factory=dict)


class Fetcher:
    def __init__(self, rate_limit: float = 1.5, timeout: float = 15.0):
        self.rate_limit = rate_limit
        self._last_fetch: dict[str, float] = defaultdict(float)
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self.client = httpx.Client(
            headers=HEADERS,
            timeout=timeout,
            follow_redirects=True,
            verify=False,
        )

    def _domain(self, url: str) -> str:
        return urlparse(url).netloc

    def _throttle(self, domain: str) -> None:
        wait = self.rate_limit - (time.time() - self._last_fetch[domain])
        if wait > 0:
            time.sleep(wait)
        self._last_fetch[domain] = time.time()

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        if robots_url not in self._robots_cache:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                parser.read()
                self._robots_cache[robots_url] = parser
            except Exception:
                self._robots_cache[robots_url] = None
        parser = self._robots_cache[robots_url]
        if parser is None:
            return True
        return parser.can_fetch(HEADERS["User-Agent"], url)

    def detect_block(self, response: httpx.Response) -> tuple[bool, str | None]:
        if response.status_code == 429:
            return True, "rate_limited_429"
        if response.status_code == 403:
            return True, "forbidden_403"
        if response.status_code == 503:
            return True, "service_unavailable_503"

        html = response.text.lower()
        h = dict(response.headers)
        body_len = len(response.text.strip())

        # High-confidence bot walls — these phrases never appear in real
        # articles, so flag regardless of page length (catches DataDome /
        # rate-limit pages like Le Monde's "traffic identified as automated").
        if any(p in html for p in BOT_WALL_PHRASES):
            return True, "bot_wall"

        # A full-content 200 page is NOT blocked just because it embeds a
        # captcha/login widget (newsletter, comments). Only treat captcha/
        # block phrases as a real block on short "challenge/interstitial"
        # pages — a genuine block page is small, not a 100KB article.
        is_short = body_len < 2000

        # Cloudflare challenge (not just any CF-served page)
        if "cf-ray" in h and is_short and any(p in html for p in CHALLENGE_PHRASES):
            return True, "cloudflare_challenge"

        if is_short and ("recaptcha" in html or "hcaptcha" in html
                         or "captcha-container" in html):
            return True, "captcha"

        if is_short and any(p in html for p in BLOCK_PHRASES):
            return True, "access_denied"

        if response.status_code == 200 and body_len < 300:
            return True, "suspicious_empty_body"

        return False, None

    def fetch(self, url: str, retries: int = 2) -> FetchResult:
        if not self.can_fetch(url):
            return FetchResult(
                url=url, final_url=url, html=None, status=0,
                blocked=True, block_reason="robots_txt", elapsed=0.0,
            )

        domain = self._domain(url)
        self._throttle(domain)

        for attempt in range(retries + 1):
            try:
                t0 = time.time()
                resp = self.client.get(url)
                elapsed = time.time() - t0

                blocked, reason = self.detect_block(resp)
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    html=resp.text if not blocked else None,
                    status=resp.status_code,
                    blocked=blocked,
                    block_reason=reason,
                    elapsed=elapsed,
                    headers=dict(resp.headers),
                )
            except httpx.TimeoutException:
                if attempt == retries:
                    return FetchResult(
                        url=url, final_url=url, html=None, status=0,
                        blocked=False, block_reason="timeout", elapsed=0.0,
                    )
                time.sleep(2 ** attempt)
            except Exception as exc:
                return FetchResult(
                    url=url, final_url=url, html=None, status=0,
                    blocked=False, block_reason=str(exc), elapsed=0.0,
                )

        # unreachable but satisfies type checker
        return FetchResult(
            url=url, final_url=url, html=None, status=0,
            blocked=False, block_reason="max_retries", elapsed=0.0,
        )

    def close(self) -> None:
        self.client.close()
