"""
Two-layer deduplication:
  1. URL-level  — canonical URL normalization + seen set
  2. Content-level — exact SHA-256 hash + MinHash LSH near-duplicate detection
"""
import hashlib
import re
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

from datasketch import MinHash, MinHashLSH
from w3lib.url import canonicalize_url as w3_canonicalize

# Query params that are pure tracking noise
STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "source", "via",
    "mc_cid", "mc_eid", "_ga", "yclid",
}

NUM_PERM = 128          # MinHash permutations — higher = more accurate
LSH_THRESHOLD = 0.80   # jaccard similarity threshold for near-duplicate


def normalize_url(url: str) -> str:
    """Strip tracking params, lowercase host, remove fragments."""
    parsed = urlparse(url)
    clean_params = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if k.lower() not in STRIP_PARAMS
    ]
    clean_params.sort()  # stable order
    normalized = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path,
        parsed.params,
        urlencode(clean_params),
        "",  # drop fragment
    ))
    return w3_canonicalize(normalized)


def _text_to_minhash(text: str) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    # Shingling: overlapping 3-word windows
    words = re.sub(r"\s+", " ", text.lower()).split()
    for i in range(max(1, len(words) - 2)):
        shingle = " ".join(words[i: i + 3])
        m.update(shingle.encode())
    return m


class DedupStore:
    def __init__(self, similarity_threshold: float = LSH_THRESHOLD):
        self._seen_urls: set[str] = set()
        self._seen_hashes: set[str] = set()
        self._lsh = MinHashLSH(threshold=similarity_threshold, num_perm=NUM_PERM)
        self._doc_count = 0

    def is_duplicate_url(self, url: str) -> tuple[bool, str | None]:
        norm = normalize_url(url)
        if norm in self._seen_urls:
            return True, norm
        return False, None

    def register_url(self, url: str) -> str:
        norm = normalize_url(url)
        self._seen_urls.add(norm)
        return norm

    def is_duplicate_content(self, text: str) -> tuple[bool, str | None]:
        # Exact hash check first (fastest)
        h = hashlib.sha256(text.encode()).hexdigest()
        if h in self._seen_hashes:
            return True, f"exact_hash:{h[:12]}"

        # Near-duplicate via MinHash LSH
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

    def check_and_register(
        self, url: str, text: str
    ) -> tuple[bool, str | None]:
        """
        Returns (is_duplicate, reason).
        If not duplicate, registers both URL and content.
        """
        dup_url, reason = self.is_duplicate_url(url)
        if dup_url:
            return True, f"duplicate_url:{reason}"

        dup_content, reason = self.is_duplicate_content(text)
        if dup_content:
            self.register_url(url)  # still track the URL
            return True, f"duplicate_content:{reason}"

        self.register_url(url)
        self.register_content(text, doc_id=normalize_url(url))
        return False, None

    @property
    def stats(self) -> dict:
        return {
            "unique_urls": len(self._seen_urls),
            "unique_content_hashes": len(self._seen_hashes),
            "lsh_index_size": self._doc_count,
        }
