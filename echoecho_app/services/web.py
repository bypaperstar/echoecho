"""Keyless web access, all verified reachable from the sandbox:
WordPress wp-json search on the whitelist, DuckDuckGo HTML fallback (uddg
redirect decode), Wikipedia opensearch / list=search / extracts.
NOTE: Wikipedia's REST /page/related endpoint is decommissioned — never use it.
"""
import html as htmllib
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from echoecho_app import diagnostics

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Sandbox-verified whitelist; allrecipes/seriouseats 403 from datacenter IPs.
WP_SITES = ("https://www.recipetineats.com", "https://pinchofyum.com")
RECIPE_SITES = ("recipetineats.com", "pinchofyum.com", "bbcgoodfood.com")

WIKI_API = "https://en.wikipedia.org/w/api.php"

_TAG_RE = re.compile(r"<[^>]+>")
_DDG_RESULT_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"')
_DIAGNOSTIC_HOST_ALLOWLIST = frozenset({
    "www.recipetineats.com", "recipetineats.com", "pinchofyum.com",
    "www.pinchofyum.com", "bbcgoodfood.com", "www.bbcgoodfood.com",
    "en.wikipedia.org", "html.duckduckgo.com", "duckduckgo.com",
})


def _diagnostic_host_fields(host):
    try:
        normalized = str(host or "").strip().lower().rstrip(".")
    except Exception:
        normalized = ""
    if not normalized:
        return {"host": None}
    if normalized in _DIAGNOSTIC_HOST_ALLOWLIST:
        return {"host": normalized}
    return {
        "host": "unknown",
        "host_length": len(normalized),
        "host_fingerprint": hashlib.sha256(
            normalized.encode("utf-8", "replace")).hexdigest()[:16],
    }


def fetch(url, timeout=20):  # type: (str, float) -> str
    started = time.monotonic()
    parsed = urllib.parse.urlparse(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = getattr(resp, "status", None)
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        diagnostics.exception(
            "web.request.failed", exc=exc,
            scheme=parsed.scheme, timeout_s=timeout,
            status=getattr(exc, "code", None),
            errno=(getattr(reason, "errno", None)
                   if reason is not None else getattr(exc, "errno", None)),
            reason_type=type(reason).__name__ if reason is not None else None,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            **_diagnostic_host_fields(parsed.hostname))
        raise
    diagnostics.info(
        "web.request.finished", scheme=parsed.scheme,
        status=status, response_bytes=len(body),
        duration_ms=round((time.monotonic() - started) * 1000, 1),
        **_diagnostic_host_fields(parsed.hostname))
    return body.decode("utf-8", "replace")


def _fetch_json(url, timeout=20):
    return json.loads(fetch(url, timeout))


def wp_search(query, sites=WP_SITES, per_page=3):
    """WordPress REST search -> [{'title', 'url'}], whitelist order preserved."""
    hits = []
    for site in sites:
        url = site + "/wp-json/wp/v2/search?" + urllib.parse.urlencode(
            {"search": query, "per_page": per_page})
        try:
            items = _fetch_json(url)
        except Exception:
            continue  # one dead site must not kill the search
        if not isinstance(items, list):
            continue  # WP error responses are JSON objects, not result lists
        for item in items:
            title = htmllib.unescape(_TAG_RE.sub("", item.get("title", ""))).strip()
            if item.get("url"):
                hits.append({"title": title, "url": item["url"]})
    diagnostics.info("web.search.finished", engine="wordpress",
                     query_chars=len(query), site_count=len(sites),
                     result_count=len(hits))
    return hits


def ddg_search(query, site=None, limit=10):
    """DuckDuckGo HTML endpoint fallback; decodes //duckduckgo.com/l/?uddg= redirects."""
    q = query + (" site:%s" % site if site else "")
    page = fetch("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q}))
    urls = []
    for m in _DDG_RESULT_RE.finditer(page):
        href = htmllib.unescape(m.group(1))
        if "uddg=" in href:
            target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            href = target.get("uddg", [""])[0]
        if href.startswith("http"):
            urls.append(href)
    result = urls[:limit]
    diagnostics.info("web.search.finished", engine="duckduckgo",
                     query_chars=len(query), site_restricted=bool(site),
                     result_count=len(result))
    return result


def wiki_opensearch(query, limit=5):
    """Fuzzy title resolution -> list of page titles."""
    url = WIKI_API + "?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": query, "limit": limit, "format": "json"})
    data = _fetch_json(url)
    result = list(data[1]) if len(data) > 1 else []
    diagnostics.info("web.search.finished", engine="wikipedia_opensearch",
                     query_chars=len(query), result_count=len(result))
    return result


def wiki_search(query, limit=8):
    """Action API full-text search -> subtopic titles."""
    url = WIKI_API + "?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "srlimit": limit, "format": "json"})
    data = _fetch_json(url)
    result = [r["title"] for r in data.get("query", {}).get("search", [])]
    diagnostics.info("web.search.finished", engine="wikipedia_search",
                     query_chars=len(query), result_count=len(result))
    return result


def wiki_extract(title, chars=1200):  # API caps exchars at 1200
    """Plaintext intro of a page via prop=extracts&exintro&explaintext."""
    url = WIKI_API + "?" + urllib.parse.urlencode(
        {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
         "exchars": chars, "redirects": 1, "titles": title, "format": "json"})
    data = _fetch_json(url)
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if page.get("extract"):
            return page["extract"]
    return ""


def wiki_url(title):
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
