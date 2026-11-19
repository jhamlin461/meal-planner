from dataclasses import dataclass
from datetime import date, timedelta

from app.ai import AIPaused
from app.setup_grocy import ids

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


def build_context(grocy, monday: date) -> dict:
    _check_monday(monday)
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    stock = grocy.stock_amounts()
    positions: dict[int, list[dict]] = {}
    for pos in grocy.objects("recipes_pos"):
        positions.setdefault(int(pos["recipe_id"]), []).append(pos)
    recipes_by_id = {int(r["id"]): r["name"] for r in user_recipes(grocy)}
    recipes = []
    for rid, name in recipes_by_id.items():
        ps = positions.get(rid, [])
        ok = sum(stock.get(int(p["product_id"]), 0.0) >= float(p["amount"]) - 1e-6 for p in ps)
        recipes.append({"id": rid, "name": name,
                        "ingredients": [names.get(int(p["product_id"]), "?") for p in ps],
                        "fulfillment": round(ok / len(ps), 2) if ps else 0.0})
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


def suggest(grocy, ai, monday: date) -> list[Pick]:
    """Ask the AI, keep only first suggestion per open slot with a real recipe. Free mode: local_picks."""
    open_slots = set(week_slots(grocy, monday))
    valid = {int(r["id"]) for r in user_recipes(grocy)}
    ctx = build_context(grocy, monday)
    try:
        items = ai.suggest_week(ctx).items
    except AIPaused:
        return local_picks(ctx)
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


def local_picks(ctx: dict) -> list[Pick]:
    """Without Claude: recipes using the most expiring stock first (then the most in stock), none cooked in the last
    3 weeks, one per open slot in order, no repeats."""
    expiring = {e["product"] for e in ctx["expiring"]}
    recent = set(ctx["recent_recipes"])
    recipes = sorted((r for r in ctx["recipes"] if r["name"] not in recent),
                     key=lambda r: (-len(expiring & set(r["ingredients"])), -r["fulfillment"]))
    out = []
    for (day, section), r in zip(ctx["open_slots"], recipes):
        used = sorted(expiring & set(r["ingredients"]))
        reason = f"Uses {', '.join(used)} before it expires" if used else "Not cooked in the last 3 weeks"
        out.append(Pick(date.fromisoformat(day), section, r["id"], reason))
    return out
