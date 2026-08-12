"""grocery.merge: merge ingredients/edits into workspace/grocery.md.

LLM path dedupes and groups by aisle; keyless fallback strips quantities with
a regex and merges case-insensitively. Both keep a '## Meals' section.
"""
import re

from echo_app.bus import TaskResult
from echo_app.services import artifacts
from echo_app.services import llm as llm_mod
from echo_app.workers.base import register

PROMPT = """You maintain a grocery list as markdown. Merge the new items below into the
current list: deduplicate (combine quantities, e.g. '2 cloves garlic' + '3 cloves
garlic' -> 'garlic (5 cloves)' — illustrative only: NEVER add items that aren't in
the new items, the instruction, or the current list), group items under
'## <Aisle>' headings, and keep a '## Meals' section listing source recipes as
markdown links. Omit the '## Meals' section entirely if there are no recipes yet.
Also apply this instruction if present: {instruction}
New items: {ingredients}
Source recipe: {source}

Current list:
---
{current}
---
Return ONLY the complete updated markdown, no commentary, no code fences."""

# Leading quantity + optional unit, e.g. "2 cloves ", "300g ", "1/2 cup ".
_QTY_RE = re.compile(
    r"^[\d\s/.,¼½¾⅓⅔-]*\s*"
    r"(?:g|kg|ml|l|oz|ounces?|lbs?|pounds?|cups?|tbsp|tablespoons?|tsp|teaspoons?|"
    r"cloves?|cans?|packs?|bunch(?:es)?|slices?|pieces?|sprigs?)?\.?\s+",
    re.IGNORECASE)


def _norm(line):
    """'2 cloves Garlic' -> 'garlic': the dedup key."""
    line = line.strip().lstrip("-*").strip()
    return (_QTY_RE.sub("", line) or line).strip().lower()


def _regex_merge(current, ingredients, source):
    """Keyless fallback: quantity-strip + case-insensitive dedup, flat list."""
    lines = current.splitlines() if current.strip() else ["# Grocery List", ""]
    if not any(l.strip() == "## Meals" for l in lines):
        lines += ["", "## Meals"]
    meals_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Meals")
    existing = {_norm(l) for l in lines if l.strip().startswith("- ")}

    added, dupes = [], 0
    for item in ingredients:
        if _norm(item) in existing:
            dupes += 1
        else:
            existing.add(_norm(item))
            added.append("- " + item.strip())
    lines[meals_idx:meals_idx] = added  # items live above ## Meals
    if source:
        meal = "- [%s](%s)%s" % (source.get("title"), source.get("url"),
                                 " (%d min)" % source["minutes"] if source.get("minutes") else "")
        if meal not in lines:
            lines.append(meal)
    return "\n".join(lines).strip() + "\n", len(added), dupes


@register("grocery.merge")
async def run(task, ctx):
    ingredients = list(task.request.args.get("ingredients", []))
    source = task.request.args.get("source_recipe")
    instruction = task.request.instructions or ""
    current = artifacts.read(ctx.workspace, "grocery.md")
    llm = llm_mod.for_ctx(ctx)
    try:
        merged = llm_mod.strip_fences(await llm.complete("grocery.merge", PROMPT.format(
            instruction=instruction or "(none)", ingredients=ingredients,
            source=source or "(none)", current=current))) + "\n"
        say = ("Merged %d items into your grocery list." % len(ingredients)
               if ingredients else "Updated your grocery list: %s." % instruction)
    except llm_mod.LLMUnavailable:
        merged, added, dupes = _regex_merge(current, ingredients, source)
        say = "Added %d items to your grocery list" % added
        say += " — %d you already had." % dupes if dupes else "."
    artifacts.write_atomic(ctx.workspace, "grocery.md", merged)
    return TaskResult(
        say=say,
        # a user-requested edit is a primary result; recipe-chained merges are enrichment
        priority="interrupt" if task.request.source == "user" else "ambient",
        data={"ingredients_in": len(ingredients)},
        artifacts_touched=["grocery.md"])
