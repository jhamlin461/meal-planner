import logging
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from app.grocy import GrocyError
from app.setup_grocy import ids
from app.shopping import rebuild_meal_plan_list

LEFTOVER_PREFIX = "Leftovers: "
log = logging.getLogger(__name__)


@dataclass
class DayPlan:
    day: date
    lunch: list[dict]
    dinner: list[dict]


def default_servings(day: date, section: str) -> int:
    return 4 if section == "Dinner" and day.weekday() in (6, 0, 1, 2, 3) else 2


def _is_leftover_dinner(day: date, section: str) -> bool:
    return section == "Dinner" and default_servings(day, section) == 4


def plan_meal(grocy, day: date, section: str, recipe_id: int, servings: int | None, settings,
              today: date | None = None) -> int:
    sec = ids(grocy).section
    entry_id = grocy.create("meal_plan", {
        "day": day.isoformat(), "type": "recipe", "recipe_id": recipe_id,
        "recipe_servings": servings or default_servings(day, section),
        "section_id": sec[section], "note": "", "done": 0})
    if _is_leftover_dinner(day, section):
        name = grocy.get_object("recipes", recipe_id)["name"]
        grocy.create("meal_plan", {
            "day": (day + timedelta(days=1)).isoformat(), "type": "note",
            "section_id": sec["Lunch"], "note": LEFTOVER_PREFIX + name, "done": 0})
    rebuild_meal_plan_list(grocy, today or date.today(), settings.shopping_weekday)
    return entry_id


def unplan_meal(grocy, entry_id: int, settings, today: date | None = None) -> None:
    entry = grocy.get_object("meal_plan", entry_id)
    if entry is None:
        return
    sec = ids(grocy).section
    day = date.fromisoformat(str(entry["day"])[:10])
    grocy.delete("meal_plan", entry_id)
    if entry.get("type") == "recipe" and int(entry["section_id"]) == sec["Dinner"] \
            and _is_leftover_dinner(day, "Dinner"):
        recipe = grocy.get_object("recipes", int(entry["recipe_id"]))
        note = LEFTOVER_PREFIX + (recipe["name"] if recipe else "")
        nxt = (day + timedelta(days=1)).isoformat()
        for r in grocy.objects("meal_plan", type="note"):
            if str(r["day"])[:10] == nxt and int(r["section_id"]) == sec["Lunch"] and r["note"] == note:
                grocy.delete("meal_plan", int(r["id"]))
                break  # only one leftover note per dinner
    rebuild_meal_plan_list(grocy, today or date.today(), settings.shopping_weekday)


def cook(grocy, entry_id: int) -> list[str]:
    entry = grocy.get_object("meal_plan", entry_id)
    if entry is None or entry.get("type") != "recipe" or int(float(entry.get("done") or 0)):
        return []
    rid = int(entry["recipe_id"])
    recipe = grocy.get_object("recipes", rid) or {}
    factor = float(entry["recipe_servings"]) / (float(recipe.get("base_servings") or 0) or 1.0)
    stock = grocy.stock_amounts()
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    needs: dict[int, float] = {}
    for pos in grocy.objects("recipes_pos", recipe_id=rid):  # a product can repeat across positions
        pid = int(pos["product_id"])
        needs[pid] = needs.get(pid, 0.0) + float(pos["amount"]) * factor
    grocy.update("meal_plan", entry_id, {"done": 1})  # first: a retry after a failed consume must not deduct twice
    short = []
    for pid, need in needs.items():
        have = stock.get(pid, 0.0)
        use = min(need, have)
        try:
            if use > 0:
                grocy.consume(pid, use)
            failed = False
        except (GrocyError, httpx.HTTPError):
            log.warning("cook %s: consume of product %s failed", entry_id, pid, exc_info=True)
            failed = True
        if failed or need - have > 1e-6:  # either way the pantry needs a look
            short.append(names.get(pid, str(pid)))
    return short


def week_view(grocy, monday: date) -> list[DayPlan]:
    lunch_id = ids(grocy).section["Lunch"]
    rows = grocy.objects("meal_plan")
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        today_rows = [r for r in rows if str(r["day"])[:10] == d.isoformat()]
        days.append(DayPlan(d, [r for r in today_rows if int(r["section_id"]) == lunch_id],
                            [r for r in today_rows if int(r["section_id"]) != lunch_id]))
    return days
