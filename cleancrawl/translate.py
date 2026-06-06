"""
Optional English translation for non-English articles (--translate flag).

Uses deep-translator's free Google endpoint (no API key). Long text is chunked
to stay under the per-request length limit, and failures degrade gracefully
(return None) so a translation hiccup never breaks the crawl.
"""
from deep_translator import GoogleTranslator

_MAX_CHUNK = 4500          # Google free endpoint limit is ~5000 chars
_MAX_TEXT = 20000          # cap body translation so one huge article can't stall the crawl


def _translate(text: str | None) -> str | None:
    if not text:
        return None
    text = text[:_MAX_TEXT]
    try:
        chunks = [text[i:i + _MAX_CHUNK] for i in range(0, len(text), _MAX_CHUNK)]
        out = [GoogleTranslator(source="auto", target="en").translate(c) or "" for c in chunks]
        result = " ".join(p for p in out if p).strip()
        return result or None
    except Exception as e:
        print(f"[Translate] failed: {e}")
        return None


def translate_article(record: dict) -> dict:
    """
    Add English translations to an article record in-place and return it.
    Adds: title_en, summary_en, main_text_en, translated_to='en'.
    No-op (returns unchanged) if the article is already English.
    """
    lang = (record.get("language") or "").lower()
    if lang.startswith("en"):
        return record  # already English — nothing to do

    record["title_en"] = _translate(record.get("title"))
    record["summary_en"] = _translate(record.get("summary"))
    record["main_text_en"] = _translate(record.get("main_text"))
    record["translated_to"] = "en"
    return record
