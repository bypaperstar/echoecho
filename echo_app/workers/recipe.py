"""recipe.search: WP REST whitelist search -> recipe-scrapers -> best pick.

Chains grocery.merge purely via the generic follow_ups mechanism; this module
is the only place that knows a recipe produces ingredients.
"""
import asyncio
import urllib.parse

from echo_app.bus import TaskRequest, TaskResult
from echo_app.services import web as web_mod
from echo_app.workers.base import register


def _web(ctx):
    return ctx.extra.get("web") or web_mod


def _scrape(html, url):
    from recipe_scrapers import scrape_html  # lazy; heavy import
    s = scrape_html(html, org_url=url)
    minutes = None
    try:
        minutes = s.total_time()
    except Exception:
        pass
    return {"title": s.title(), "url": url, "minutes": minutes,
            "ingredients": list(s.ingredients())}


def _site_name(url):
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    names = {"recipetineats.com": "RecipeTin Eats", "pinchofyum.com": "Pinch of Yum",
             "bbcgoodfood.com": "BBC Good Food"}
    return names.get(host, host)


@register("recipe.search")
async def run(task, ctx):
    web = _web(ctx)
    query = task.request.instructions or task.request.args.get("dish", "")
    hits = await asyncio.to_thread(web.wp_search, query)
    if not hits:  # verified fallback: DDG HTML endpoint scoped to the whitelist
        for site in web_mod.RECIPE_SITES:
            urls = await asyncio.to_thread(web.ddg_search, query + " recipe", site, 2)
            hits.extend({"title": query, "url": u} for u in urls)

    candidates = []
    for hit in hits[:4]:
        try:
            html = await asyncio.to_thread(web.fetch, hit["url"])
            candidates.append(_scrape(html, hit["url"]))
        except Exception:
            continue  # unscrapable page: try the next hit
    if not candidates:
        return TaskResult(say="I couldn't find a usable recipe for %s." % query,
                          priority="interrupt", data={"error": "no recipe found",
                                                      "query": query})

    max_minutes = task.request.args.get("max_minutes")
    pool = [c for c in candidates
            if not max_minutes or (c["minutes"] and c["minutes"] <= max_minutes)]
    best = min(pool or candidates, key=lambda c: c["minutes"] or 9999)

    say = "Found a %s%s on %s — adding %d ingredients to your grocery list." % (
        ("%d-minute " % best["minutes"]) if best["minutes"] else "",
        best["title"], _site_name(best["url"]), len(best["ingredients"]))
    return TaskResult(
        say=say, priority="interrupt", data=best,
        follow_ups=[TaskRequest(
            kind="grocery.merge",
            instructions="add ingredients for %s" % best["title"],
            args={"ingredients": best["ingredients"],
                  "source_recipe": {"title": best["title"], "url": best["url"],
                                    "minutes": best["minutes"]}})])
