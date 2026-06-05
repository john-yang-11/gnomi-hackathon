"""
Classifies URLs and pages as article-like or junk (nav, tag, category, etc).
Supports multilingual sites — URL path patterns in major world languages.
"""
import re
from urllib.parse import urlparse, parse_qs, unquote

# URL path segments that signal non-article pages, across languages.
# English + Spanish + French + German + Portuguese + Italian + common CJK romanizations.
JUNK_PATH_SEGMENTS = re.compile(
    r"/("
    # English
    r"tag|tags|category|categories|author|authors|search|find|login|signin|"
    r"register|signup|subscribe|feed|rss|sitemap|cart|checkout|account|profile|"
    r"comment|comments|reply|replies|archive|archives|page|pagination|"
    r"print|amp|mobile|embed|share|widget|banner|ad|ads|popup|"
    # Spanish
    r"etiqueta|etiquetas|categoria|categorias|autor|autores|buscar|busqueda|"
    r"acceder|registrarse|suscribirse|comentario|comentarios|archivo|pagina|carro|"
    # French
    r"etiquette|categorie|categories|auteur|auteurs|recherche|connexion|"
    r"inscription|abonnement|commentaire|commentaires|archives|imprimer|panier|"
    # German
    r"schlagwort|kategorie|kategorien|autor|suche|anmelden|registrieren|"
    r"abonnieren|kommentar|kommentare|archiv|seite|drucken|warenkorb|"
    # Portuguese
    r"etiqueta|categoria|categorias|autor|autores|busca|pesquisa|entrar|"
    r"cadastro|assinar|comentario|comentarios|arquivo|imprimir|carrinho|"
    # Italian
    r"categoria|categorie|autore|autori|cerca|ricerca|accedi|registrati|"
    r"abbonati|commento|commenti|archivio|pagina|stampa|carrello"
    r")s?(/|$)",
    re.IGNORECASE,
)

# Pure pagination patterns like /page/2, /p/3, ?page=4 (plus localized page words)
PAGINATION = re.compile(
    r"(/page/\d+|/p/\d+|/pagina/\d+|/seite/\d+|"
    r"\?page=\d+|\?pagina=\d+|\?seite=\d+|\?offset=\d+|\?start=\d+|/\d+/$)",
    re.IGNORECASE,
)

# Calendar trap: /2026/06/ with no slug after
CALENDAR_TRAP = re.compile(r"/\d{4}/\d{2}/?$")

# Junk query parameters
JUNK_PARAMS = {
    "s", "q", "query", "search", "sort", "order", "filter", "replytocom",
    "buscar", "busqueda", "recherche", "suche", "pesquisa", "ricerca",
}

# URL patterns that strongly suggest an article, across languages.
ARTICLE_HINTS = re.compile(
    r"/("
    # English
    r"article|articles|news|post|posts|blog|story|stories|"
    r"wiki|docs|documentation|guide|guides|report|analysis|"
    r"opinion|editorial|feature|explainer|review|reviews|"
    # Spanish
    r"articulo|articulos|noticia|noticias|entrada|reportaje|"
    r"analisis|opinion|cronica|reportajes|"
    # French
    r"article|articles|actualite|actualites|actu|nouvelle|nouvelles|"
    r"billet|reportage|analyse|chronique|dossier|"
    # German
    r"artikel|nachricht|nachrichten|beitrag|meldung|bericht|"
    r"reportage|analyse|kolumne|"
    # Portuguese
    r"artigo|artigos|noticia|noticias|materia|reportagem|"
    r"analise|cronica|"
    # Italian
    r"articolo|articoli|notizia|notizie|cronaca|reportage|"
    r"analisi|approfondimento|"
    # CJK romanized (common in URL slugs)
    r"kiji|shimbun|news|noticias|"
    # Arabic/other romanized
    r"akhbar|maqal|maqala"
    r")(/|$|-)",
    re.IGNORECASE,
)

# Non-Latin script ranges — presence in a slug signals real content
# (CJK, Hiragana, Katakana, Hangul, Arabic, Cyrillic, Hebrew, Thai, Devanagari)
NON_LATIN_SCRIPT = re.compile(
    r"[Ѐ-ӿ"      # Cyrillic
    r"֐-׿"       # Hebrew
    r"؀-ۿ"       # Arabic
    r"ऀ-ॿ"       # Devanagari
    r"฀-๿"       # Thai
    r"぀-ヿ"       # Hiragana + Katakana
    r"㐀-䶿"       # CJK Extension A
    r"一-鿿"       # CJK Unified
    r"가-힯]"      # Hangul
)

# JSON-LD types that confirm article content
ARTICLE_SCHEMA_TYPES = {
    "NewsArticle", "Article", "BlogPosting", "TechArticle",
    "ScholarlyArticle", "Report", "WebPage",
}


def _has_article_slug(path: str) -> bool:
    """
    Path ends with a slug containing real words (not just numbers/id).
    Handles non-Latin scripts and percent-encoded URLs (e.g. Japanese, Arabic).
    """
    slug = path.rstrip("/").split("/")[-1]
    # Decode percent-encoding so %E8%A8%98 becomes the actual CJK characters
    decoded = unquote(slug)

    # Non-Latin script slug (Japanese, Chinese, Arabic, Cyrillic, etc.)
    if NON_LATIN_SCRIPT.search(decoded):
        return len(decoded) >= 2  # even 2 CJK chars can be a title

    # Latin-script slug: at least 3 letters and a reasonable length
    return bool(re.search(r"[a-z]{3,}", decoded, re.IGNORECASE)) and len(decoded) > 5


def classify_url(url: str) -> tuple[str, str]:
    """
    Returns (page_type, reason).
    page_type: 'article' | 'skip'
    """
    parsed = urlparse(url)
    # Decode percent-encoding so non-Latin paths (Japanese, Arabic, etc.) match
    path = unquote(parsed.path).lower()
    params = parse_qs(parsed.query)

    if JUNK_PATH_SEGMENTS.search(path):
        return "skip", "junk_path_segment"

    if PAGINATION.search(url):
        return "skip", "pagination"

    if CALENDAR_TRAP.search(path):
        return "skip", "calendar_trap"

    junk_qs = set(params.keys()) & JUNK_PARAMS
    if junk_qs:
        return "skip", f"junk_query_param:{','.join(junk_qs)}"

    # Explicit article path hint
    if ARTICLE_HINTS.search(path):
        return "article", "article_path_hint"

    # Has a meaningful slug
    if _has_article_slug(path):
        return "article", "article_slug"

    return "skip", "no_article_signal"


def classify_page(html: str, url: str) -> tuple[str, str]:
    """
    Refine classification using page content (JSON-LD, meta tags).
    Returns (page_type, reason).
    """
    import json
    from lxml import etree

    url_type, url_reason = classify_url(url)

    try:
        parser = etree.HTMLParser()
        tree = etree.fromstring(html.encode(), parser)
    except Exception:
        return url_type, url_reason

    # Check JSON-LD schema type
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text or "")
            schema_type = data.get("@type", "")
            if isinstance(schema_type, list):
                schema_type = schema_type[0]
            if schema_type in ARTICLE_SCHEMA_TYPES:
                return "article", f"schema_org:{schema_type}"
        except Exception:
            continue

    # Check og:type
    og_type = tree.xpath('//meta[@property="og:type"]/@content')
    if og_type and "article" in og_type[0].lower():
        return "article", "og_type_article"

    return url_type, url_reason
