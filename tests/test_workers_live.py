"""Live keyless endpoints (sandbox-verified reachable). Deselected by default
via pytest.ini addopts; run with: python3 -m pytest -m network tests/test_workers_live.py"""
import pytest

from echo_app.services import web

pytestmark = pytest.mark.network


def test_wp_search_pad_thai_returns_whitelisted_url():
    hits = web.wp_search("pad thai")
    assert hits, "WP search returned nothing"
    assert any("recipetineats.com" in h["url"] or "pinchofyum.com" in h["url"]
               for h in hits), hits


def test_recipe_scrapers_extracts_ingredients_live():
    from recipe_scrapers import scrape_html
    hits = web.wp_search("pad thai")
    url = next(h["url"] for h in hits
               if "recipetineats.com" in h["url"] or "pinchofyum.com" in h["url"])
    scraper = scrape_html(web.fetch(url), org_url=url)
    ingredients = scraper.ingredients()
    assert len(ingredients) >= 5, ingredients
    assert scraper.title()


def test_wikipedia_search_returns_fermentation_subtopics():
    titles = web.wiki_search("fermentation food")
    assert titles, "Wikipedia list=search returned nothing"
    assert any("ferment" in t.lower() for t in titles), titles
    extract = web.wiki_extract(titles[0])
    assert len(extract) > 100
