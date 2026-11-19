from dataclasses import dataclass
from datetime import date, timedelta

from app.ai import AIPaused
from app.inventory import free_stock, kits, stock_lines
from app.mealplan import default_servings
from app.setup_grocy import ids
from app.shopping import base_servings

NEVER = "2999-12-31"  # Grocy's "never expires"


@dataclass
class Pick:
    """A validated suggestion; like ai.Suggestion but with `day` as a date."""
    day: date
    section: str
    recipe_id: int
    reason: str


def user_recipes(grocy) -> list[dict]:
    """The household's recipes; Grocy keeps its internal meal-plan day/week/shadow recipes in the same table."""
    return [r for r in grocy.objects("recipes") if (r.get("type") or "normal") == "normal"]


def _check_monday(monday: date) -> None:
    if monday.weekday() != 0:
        raise ValueError(f"{monday} is not a Monday")


def _recipe_rows(grocy):
    sec = ids(grocy).section
    return [r for r in grocy.objects("meal_plan") if r.get("type") == "recipe"], sec


def week_slots(grocy, monday: date) -> list[tuple[date, str]]:
    """Open slots: 7 dinners + Sat/Sun lunch, minus those with a recipe already planned."""
    _check_monday(monday)
    rows, sec = _recipe_rows(grocy)
    taken = {(str(r["day"])[:10], int(r["section_id"])) for r in rows}
    slots = []
    for i in range(7):
        d = monday + timedelta(days=i)
        for section in (("Lunch", "Dinner") if d.weekday() >= 5 else ("Dinner",)):
            if (d.isoformat(), sec[section]) not in taken:
                slots.append((d, section))
    return slots


def build_context(grocy, monday: date, servings: int | None = None, house=None) -> dict:
    """`servings`: scale each recipe to it for fulfillment (None: its own base). With `house`, stock is what no meal kit
    holds plus the recipe's own kit; a recipe with a kit in inventory is fully stocked (spec call 10)."""
    _check_monday(monday)
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    positions: dict[int, list[dict]] = {}
    for pos in grocy.objects("recipes_pos"):
        positions.setdefault(int(pos["recipe_id"]), []).append(pos)
    user = user_recipes(grocy)
    recipes_by_id = {int(r["id"]): r["name"] for r in user}
    has_kit = set(kits(grocy, house)) if house is not None else set()
    plain = grocy.stock_amounts() if house is None else free_stock(grocy, house)
    recipes = []
    for r in user:
        rid = int(r["id"])
        ps = positions.get(rid, [])
        stock = free_stock(grocy, house, recipes=(rid,)) if rid in has_kit else plain
        scale = servings / base_servings(r) if servings else 1.0
        lines, _ = stock_lines(grocy, rid, scale)  # the amounts cooking and shopping use; no amount means ignored
        ok = sum(stock.get(l["pid"], 0.0) >= l["amount"] - 1e-6 for l in lines)
        full = rid in has_kit or bool(lines) and ok == len(lines)
        recipes.append({"id": rid, "name": r["name"],
                        "ingredients": [names.get(int(p["product_id"]), "?") for p in ps],
                        "fulfillment": 1.0 if rid in has_kit else round(ok / len(lines), 2) if lines else 0.0,
                        "fully_stocked": full, "kit": rid in has_kit})
    horizon = (date.today() + timedelta(days=7)).isoformat()
    expiring = sorted(
        ({"product": names.get(int(e["product_id"]), "?"), "best_before_date": str(e["best_before_date"])[:10]}
         for e in grocy.stock_entries()
         if e.get("best_before_date") and NEVER > str(e["best_before_date"])[:10] <= horizon),
        key=lambda x: x["best_before_date"])
    start = (monday - timedelta(days=21)).isoformat()
    recent = sorted({recipes_by_id[int(r["recipe_id"])] for r in grocy.objects("meal_plan")
                     if r.get("type") == "recipe" and start <= str(r["day"])[:10] < monday.isoformat()
                     and int(r["recipe_id"]) in recipes_by_id})
    return {"recipes": recipes, "expiring": expiring, "recent_recipes": recent,
            "open_slots": [[d.isoformat(), s] for d, s in week_slots(grocy, monday)]}


def suggest(grocy, ai, monday: date, from_stock: bool = False, house=None) -> list[Pick]:
    """Ask the AI, keep only first suggestion per open slot with a real recipe. Free mode: local_picks.
    `from_stock`: fill the slots from what the household has (fully stocked recipes first)."""
    open_slots = set(week_slots(grocy, monday))
    valid = {int(r["id"]) for r in user_recipes(grocy)}
    ctx = build_context(grocy, monday, default_servings(monday, "Dinner") if from_stock else None, house)
    try:
        items = (ai.suggest_week(ctx, from_stock=True) if from_stock else ai.suggest_week(ctx)).items
    except AIPaused:
        products = {int(p["id"]): p for p in grocy.objects("products")}
        generic = {p["name"]: products[int(p["parent_product_id"])]["name"] for p in products.values()
                   if p.get("parent_product_id") and int(p["parent_product_id"]) in products}
        return local_picks(ctx, generic, from_stock)
    out, seen = [], set()
    for s in items:
        try:
            slot = (date.fromisoformat(s.day), s.section)
        except ValueError:
            continue
        if s.recipe_id in valid and slot in open_slots and slot not in seen:
            seen.add(slot)
            out.append(Pick(slot[0], s.section, s.recipe_id, s.reason))
    return out


def local_picks(ctx: dict, generic: dict[str, str] | None = None, from_stock: bool = False) -> list[Pick]:
    """Without Claude: recipes using the most expiring stock first (then the most in stock), none cooked in the last
    3 weeks, one per open slot in order, no repeats. `generic` maps a brand product's name to its generic's, so
    expiring brand stock counts for recipes that use the generic. `from_stock`: meal kits first, then fully stocked
    recipes, then the most stocked, then expiring stock; a recipe cooked in the last 3 weeks only after the other
    fully stocked ones."""
    generic = generic or {}
    expiring = {generic.get(e["product"], e["product"]) for e in ctx["expiring"]}
    recent = set(ctx["recent_recipes"])
    if from_stock:
        recipes = sorted(ctx["recipes"], key=lambda r: (not r.get("kit"), not r.get("fully_stocked"), r["name"] in recent,
                                                        -r["fulfillment"], -len(expiring & set(r["ingredients"]))))
    else:
        recipes = sorted((r for r in ctx["recipes"] if r["name"] not in recent),
                         key=lambda r: (-len(expiring & set(r["ingredients"])), -r["fulfillment"]))
    out = []
    for (day, section), r in zip(ctx["open_slots"], recipes):
        used = sorted(expiring & set(r["ingredients"]))
        if from_stock and r.get("kit"):
            reason = "Your meal kit is here"
        elif from_stock and r.get("fully_stocked"):
            reason = "You have everything"
        elif from_stock and r["fulfillment"]:
            reason = "You have most of it" if r["fulfillment"] >= 0.5 else "You have some of it"
        else:
            reason = f"Uses {', '.join(used)} before it expires" if used else "Not cooked in the last 3 weeks"
        out.append(Pick(date.fromisoformat(day), section, r["id"], reason))
    return out
