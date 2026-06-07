"""
Layered deduplication (cheapest check first, first hit wins):
  1. URL-level   — aggressive normalization (www / AMP / mobile / print /
                   scheme / trailing-slash / tracking params) + seen set
  2. Exact       — SHA-256 of the clean article text
  3. Near-dup    — MinHash LSH at 80% Jaccard (CJK-aware shingling)
  4. Title+date  — same normalized title published on the same date
                   (catches syndicated copies the body-similarity missed)
"""
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

from datasketch import MinHash, MinHashLSH

# Query params that are pure tracking noise — never change the content.
STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "ref", "ref_src",
    "source", "via", "mc_cid", "mc_eid", "_ga", "yclid", "igshid", "spm",
    "cmpid", "campaign", "outputtype",
}

# Path segments that mark an ALTERNATE rendering of the same article.
ALT_PATH_SEGMENTS = {"amp", "mobile", "m", "print", "printable"}

NUM_PERM = 128          # MinHash permutations — higher = more accurate
LSH_THRESHOLD = 0.80    # Jaccard similarity threshold for near-duplicate

# CJK / no-space scripts — need character n-gram shingling, not word splitting.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def _strip_alt_segments(path: str) -> str:
    """Drop /amp, /mobile, /print etc. path segments."""
    parts = [p for p in path.split("/") if p]
    kept = [p for p in parts if p.lower() not in ALT_PATH_SEGMENTS]
    return "/" + "/".join(kept) if kept else "/"


def normalize_url(url: str) -> str:
    """
    Canonical dedup KEY for a URL. Collapses the many shapes the same article
    appears under so URL-level dedup catches them all:
      - tracking params stripped, remaining params sorted
      - host lowercased, leading ``www.`` removed
      - http and https unified (https)
      - default ports dropped
      - AMP / mobile / print path segments removed
      - trailing slash and fragment removed
    Purely a dedup key — does not affect what gets fetched or stored.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url

    parts = urlsplit(url)

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = f"{host}:{port}" if port and port not in (80, 443) else host

    path = _strip_alt_segments(parts.path)
    if len(path) > 1:
        path = path.rstrip("/")

    clean = sorted(
        (k, v) for k, v in parse_qsl(parts.query)
        if k.lower() not in STRIP_PARAMS
    )
    query = urlencode(clean)

    # Unify scheme to https for keying (same content served on http & https).
    return urlunsplit(("https", netloc, path, query, ""))


def _shingles(text: str):
    """
    Yield shingles for MinHash. Latin text → overlapping 3-WORD windows;
    CJK text → overlapping 3-CHARACTER windows (CJK has no word spaces, so
    word-splitting would produce a single useless token).
    """
    text = text.lower()
    if _CJK_RE.search(text[:200]):
        chars = re.sub(r"\s+", "", text)
        for i in range(max(1, len(chars) - 2)):
            yield chars[i:i + 3]
    else:
        words = re.sub(r"\s+", " ", text).split()
        for i in range(max(1, len(words) - 2)):
            yield " ".join(words[i:i + 3])


def _text_to_minhash(text: str) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    for shingle in _shingles(text):
        m.update(shingle.encode())
    return m


def _norm_title(title: str | None) -> str | None:
    """Normalize a title for comparison: drop trailing ' | Site Name', lowercase,
    strip punctuation, collapse whitespace."""
    if not title:
        return None
    t = title
    if " | " in t:
        t = t.rsplit(" | ", 1)[0]   # strip the site-name suffix many outlets append
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def _norm_date(publish_date: str | None) -> str | None:
    """Reduce a publish date to YYYY-MM-DD (ignore time/zone differences)."""
    if not publish_date:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", publish_date)
    return m.group(0) if m else publish_date[:10] or None


class DedupStore:
    def __init__(self, similarity_threshold: float = LSH_THRESHOLD):
        self._seen_urls: set[str] = set()
        self._seen_hashes: set[str] = set()
        self._seen_title_date: dict[tuple[str, str], str] = {}
        self._lsh = MinHashLSH(threshold=similarity_threshold, num_perm=NUM_PERM)
        self._doc_count = 0

    # ── URL layer ────────────────────────────────────────────
    def is_duplicate_url(self, url: str) -> tuple[bool, str | None]:
        norm = normalize_url(url)
        if norm in self._seen_urls:
            return True, norm
        return False, None

    def register_url(self, url: str) -> str:
        norm = normalize_url(url)
        self._seen_urls.add(norm)
        return norm

    # ── Title+date layer ─────────────────────────────────────
    @staticmethod
    def _title_date_key(title, publish_date) -> tuple[str, str] | None:
        nt, nd = _norm_title(title), _norm_date(publish_date)
        return (nt, nd) if nt and nd else None

    def is_duplicate_title_date(self, title, publish_date) -> tuple[bool, str | None]:
        key = self._title_date_key(title, publish_date)
        if key and key in self._seen_title_date:
            return True, f"title_date:{self._seen_title_date[key]}"
        return False, None

    def register_title_date(self, title, publish_date, url: str) -> None:
        key = self._title_date_key(title, publish_date)
        if key:
            self._seen_title_date.setdefault(key, normalize_url(url))

    # ── Content layers (exact + near-dup) ────────────────────
    def is_duplicate_content(self, text: str) -> tuple[bool, str | None]:
        h = hashlib.sha256(text.encode()).hexdigest()
        if h in self._seen_hashes:
            return True, f"exact_hash:{h[:12]}"
        mh = _text_to_minhash(text)
        results = self._lsh.query(mh)
        if results:
            return True, f"near_duplicate_of:{results[0]}"
        return False, None

    def register_content(self, text: str, doc_id: str | None = None) -> str:
        h = hashlib.sha256(text.encode()).hexdigest()
        self._seen_hashes.add(h)
        self._doc_count += 1
        key = doc_id or f"doc_{self._doc_count}"
        mh = _text_to_minhash(text)
        try:
            self._lsh.insert(key, mh)
        except ValueError:
            pass  # already inserted with same key
        return h

    # ── Orchestration ────────────────────────────────────────
    def check_and_register(
        self, url: str, text: str,
        title: str | None = None, publish_date: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Returns (is_duplicate, reason). If not a duplicate, registers the URL,
        content fingerprints, and title+date so later pages can match it.
        Checks run cheapest-first; the first hit wins.
        """
        dup_url, reason = self.is_duplicate_url(url)
        if dup_url:
            return True, f"duplicate_url:{reason}"

        dup_content, reason = self.is_duplicate_content(text)
        if dup_content:
            self.register_url(url)
            return True, f"duplicate_content:{reason}"

        # Title+date last — catches syndicated copies whose body was reformatted
        # enough to fall below the near-dup threshold but share title + date.
        dup_td, reason = self.is_duplicate_title_date(title, publish_date)
        if dup_td:
            self.register_url(url)
            return True, f"duplicate_{reason}"

        self.register_url(url)
        self.register_content(text, doc_id=normalize_url(url))
        self.register_title_date(title, publish_date, url)
        return False, None

    @property
    def stats(self) -> dict:
        return {
            "unique_urls": len(self._seen_urls),
            "unique_content_hashes": len(self._seen_hashes),
            "unique_title_dates": len(self._seen_title_date),
            "lsh_index_size": self._doc_count,
        }
