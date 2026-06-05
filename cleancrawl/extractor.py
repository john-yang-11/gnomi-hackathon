"""
Content extraction using trafilatura + JSON-LD/OpenGraph metadata fallback.
Outputs a structured article dict with a quality_score.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import trafilatura
from trafilatura.settings import use_config
from lxml import etree

_config = use_config()
_config.set("DEFAULT", "MIN_EXTRACTED_SIZE", "200")
_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "200")


@dataclass
class Article:
    url: str
    canonical_url: str | None
    title: str | None
    author: str | None
    publish_date: str | None
    language: str | None
    summary: str | None
    main_text: str
    headings: list[str]
    source_domain: str
    quality_score: float
    quality_reasons: dict = field(default_factory=dict)


def _parse_tree(html: str):
    try:
        return etree.fromstring(html.encode(), etree.HTMLParser())
    except Exception:
        return None


def _jsonld_meta(tree) -> dict:
    if tree is None:
        return {}
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text or "")
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") in (
                "NewsArticle", "Article", "BlogPosting",
                "TechArticle", "ScholarlyArticle",
            ):
                return data
        except Exception:
            continue
    return {}


def _og_meta(tree) -> dict:
    if tree is None:
        return {}
    result = {}
    for meta in tree.xpath('//meta[@property]'):
        prop = meta.get("property", "")
        content = meta.get("content", "")
        if prop.startswith("og:") and content:
            result[prop[3:]] = content
    return result


def _canonical_url(tree, fallback: str) -> str:
    if tree is None:
        return fallback
    links = tree.xpath('//link[@rel="canonical"]/@href')
    return links[0] if links else fallback


def _headings(tree) -> list[str]:
    if tree is None:
        return []
    headings = []
    for tag in ("h1", "h2", "h3"):
        for el in tree.xpath(f"//{tag}"):
            text = "".join(el.itertext()).strip()
            if text and len(text) > 3:
                headings.append(text)
    return headings[:10]


# CJK character ranges — these scripts pack far more meaning per character,
# so length/sentence thresholds must be scaled down for them.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
# Sentence-ending punctuation across scripts (Latin, CJK, Arabic, Devanagari)
_SENTENCE_SPLIT = re.compile(r"[.!?。！？؟।…]+")


def _is_cjk(text: str) -> bool:
    """True if the text is predominantly CJK (sampled from first 200 chars)."""
    sample = text[:200]
    if not sample:
        return False
    cjk_count = len(_CJK_RE.findall(sample))
    return cjk_count > len(sample) * 0.2


def _freshness_score(publish_date: str | None) -> float:
    """
    0..1 based on how recent the article is.
    < 7 days = 1.0, decaying linearly to 0 at ~365 days. Unparseable/none = 0.
    """
    if not publish_date:
        return 0.0
    # Try the common ISO-ish prefixes (YYYY-MM-DD...) that JSON-LD/trafilatura emit
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(publish_date[:len(fmt) + 2].rstrip("Z"), fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(publish_date.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    age_days = (datetime.utcnow() - dt).days
    if age_days <= 7:
        return 1.0
    if age_days >= 365:
        return 0.0
    return max(0.0, 1.0 - (age_days - 7) / (365 - 7))


def _uniqueness_score(text: str) -> float:
    """
    0..1 lexical-diversity proxy (type-token ratio): unique words / total words.
    Templated/boilerplate/spam text repeats words (low TTR); real articles are
    diverse (high TTR). Sampled over the first 400 words for stability.
    """
    words = re.findall(r"\w+", text.lower())[:400]
    if len(words) < 20:
        return 0.0
    ttr = len(set(words)) / len(words)
    # Typical articles land ~0.45–0.65 TTR → map that band onto 0..1.
    return max(0.0, min((ttr - 0.25) / 0.40, 1.0))


def _quality_score(article: dict) -> tuple[float, dict]:
    reasons = {}

    text = article.get("main_text", "")
    text_len = len(text)
    cjk = _is_cjk(text)

    # COMPLETENESS / USEFULNESS ----------------------------------------------
    # Text length (0–0.25) — CJK needs ~1/3 the characters for the same content
    length_target = 1000 if cjk else 3000
    text_score = min(text_len / length_target, 1.0) * 0.25
    reasons["text_length"] = round(text_score, 3)

    # Has title (0–0.10)
    title_score = 0.10 if article.get("title") else 0.0
    reasons["has_title"] = title_score

    # Has author (0–0.10)
    author_score = 0.10 if article.get("author") else 0.0
    reasons["has_author"] = author_score

    # Has date present at all (0–0.05)
    date_score = 0.05 if article.get("publish_date") else 0.0
    reasons["has_date"] = date_score

    # Has headings (0–0.10)
    heading_score = min(len(article.get("headings", [])) / 3, 1.0) * 0.10
    reasons["headings"] = round(heading_score, 3)

    # Language detected (0–0.10)
    lang_score = 0.10 if article.get("language") else 0.0
    reasons["language_detected"] = lang_score

    # CLEANLINESS ------------------------------------------------------------
    # Low sentence count = low quality (listicle, stub). Multi-script aware.
    sentences = len([s for s in _SENTENCE_SPLIT.split(text) if s.strip()])
    sentence_target = 5 if cjk else 10
    sentence_score = min(sentences / sentence_target, 1.0) * 0.10
    reasons["sentence_count"] = round(sentence_score, 3)

    # FRESHNESS (0–0.10) — how recently it was published
    fresh = _freshness_score(article.get("publish_date")) * 0.10
    reasons["freshness"] = round(fresh, 3)

    # UNIQUENESS (0–0.10) — lexical diversity (low = templated/boilerplate/spam)
    uniq = _uniqueness_score(text) * 0.10
    reasons["uniqueness"] = round(uniq, 3)

    total = sum(reasons.values())
    return round(min(total, 1.0), 3), reasons


def _jsonld_author(jsonld: dict) -> str | None:
    """Extract author name from JSON-LD, which may be a dict, list, or string."""
    author = jsonld.get("author")
    if isinstance(author, dict):
        return author.get("name")
    if isinstance(author, list) and author:
        first = author[0]
        return first.get("name") if isinstance(first, dict) else str(first)
    if isinstance(author, str):
        return author
    return None


def _detect_language(data: dict, jsonld: dict, tree, text: str) -> str | None:
    """
    Resolve language from (in order): trafilatura, JSON-LD inLanguage,
    <html lang>, then langdetect on the text as a last resort.
    Returns a 2-letter ISO code where possible.
    """
    # 1. trafilatura
    lang = data.get("language")
    if lang:
        return lang[:2].lower()

    # 2. JSON-LD inLanguage
    in_lang = jsonld.get("inLanguage")
    if isinstance(in_lang, str) and in_lang:
        return in_lang[:2].lower()

    # 3. <html lang="...">
    if tree is not None:
        html_lang = tree.xpath("//html/@lang")
        if html_lang and html_lang[0]:
            return html_lang[0][:2].lower()

    # 4. langdetect fallback (statistical, works on any script).
    # CJK is information-dense, so a much shorter sample suffices.
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0  # deterministic
        min_chars = 15 if _CJK_RE.search(text[:100]) else 50
        if len(text) >= min_chars:
            return detect(text)
    except Exception:
        pass

    return None


def extract(html: str, url: str) -> Article | None:
    tree = _parse_tree(html)
    jsonld = _jsonld_meta(tree)
    og = _og_meta(tree)
    canonical = _canonical_url(tree, url)

    result = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_links=False,
        include_images=False,
        favor_precision=True,
        config=_config,
        url=url,
    )

    if not result:
        return None

    data = json.loads(result)
    main_text = data.get("text", "").strip()
    if not main_text or len(main_text) < 200:
        return None

    # Merge metadata: trafilatura → JSON-LD → OpenGraph
    title = (
        data.get("title")
        or jsonld.get("headline")
        or og.get("title")
    )
    author = (
        data.get("author")
        or _jsonld_author(jsonld)
        or og.get("author")
    )
    publish_date = (
        data.get("date")
        or jsonld.get("datePublished")
        or jsonld.get("dateModified")
    )
    language = _detect_language(data, jsonld, tree, main_text)
    summary = data.get("description") or og.get("description") or jsonld.get("description")

    article_dict = {
        "title": title,
        "author": author,
        "publish_date": publish_date,
        "language": language,
        "summary": summary,
        "main_text": main_text,
        "headings": _headings(tree),
    }

    score, reasons = _quality_score(article_dict)

    return Article(
        url=url,
        canonical_url=canonical if canonical != url else None,
        title=title,
        author=author,
        publish_date=publish_date,
        language=language,
        summary=summary,
        main_text=main_text,
        headings=_headings(tree),
        source_domain=urlparse(url).netloc,
        quality_score=score,
        quality_reasons=reasons,
    )
