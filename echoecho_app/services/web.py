"""Keyless web access, all verified reachable from the sandbox:
WordPress wp-json search on the whitelist, DuckDuckGo HTML fallback (uddg
redirect decode), Wikipedia opensearch / list=search / extracts.
NOTE: Wikipedia's REST /page/related endpoint is decommissioned — never use it.
"""
import html as htmllib
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Sandbox-verified whitelist; allrecipes/seriouseats 403 from datacenter IPs.
WP_SITES = ("https://www.recipetineats.com", "https://pinchofyum.com")
RECIPE_SITES = ("recipetineats.com", "pinchofyum.com", "bbcgoodfood.com")

WIKI_API = "https://en.wikipedia.org/w/api.php"

_TAG_RE = re.compile(r"<[^>]+>")
_DDG_RESULT_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"')


def fetch(url, timeout=20):  # type: (str, float) -> str
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


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
    return urls[:limit]


def wiki_opensearch(query, limit=5):
    """Fuzzy title resolution -> list of page titles."""
    url = WIKI_API + "?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": query, "limit": limit, "format": "json"})
    data = _fetch_json(url)
    return list(data[1]) if len(data) > 1 else []


def wiki_search(query, limit=8):
    """Action API full-text search -> subtopic titles."""
    url = WIKI_API + "?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "srlimit": limit, "format": "json"})
    data = _fetch_json(url)
    return [r["title"] for r in data.get("query", {}).get("search", [])]


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
