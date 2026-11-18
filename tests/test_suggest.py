from datetime import date, timedelta

import pytest

from app.ai import Suggestion, WeekSuggestions
from app.setup_grocy import ids
from app.suggest import build_context, suggest, week_slots
from tests.fakes import FakeAI

MON = date(2026, 11, 16)


def _plan(g, day, section, rid=1):
    g.create("meal_plan", {"day": day.isoformat(), "type": "recipe", "recipe_id": rid, "recipe_servings": 4,
                           "section_id": ids(g).section[section], "note": "", "done": 0})


def _empty(g):
    for r in g.objects("meal_plan"):
        g.delete("meal_plan", r["id"])
    return g


def test_week_slots_count_and_skips_planned(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    assert len(week_slots(g, MON)) == 9
    _plan(g, MON, "Dinner")
    slots = week_slots(g, MON)
    assert len(slots) == 8 and (MON, "Dinner") not in slots
    assert (date(2026, 11, 21), "Lunch") in slots and (MON, "Lunch") not in slots


def test_monday_must_be_monday(fake_grocy_with_week):
    with pytest.raises(ValueError):
        week_slots(fake_grocy_with_week, date(2026, 11, 17))


def test_suggest_drops_invalid_and_dupes(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    _plan(g, MON, "Dinner")
    tue = (MON + timedelta(days=1)).isoformat()
    ai = FakeAI(week=WeekSuggestions(items=[
        Suggestion(day=tue, section="Dinner", recipe_id=1, reason="ok"),
        Suggestion(day=tue, section="Dinner", recipe_id=1, reason="dupe"),
        Suggestion(day=tue, section="Dinner", recipe_id=999, reason="unknown recipe"),
        Suggestion(day=MON.isoformat(), section="Dinner", recipe_id=1, reason="taken"),
    ]))
    [s] = suggest(g, ai, MON)
    assert (s.day, s.section, s.recipe_id, s.reason) == (date(2026, 11, 17), "Dinner", 1, "ok")


def test_build_context(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    today = date.today()
    g.add_stock(10, 5, today + timedelta(days=2), None)
    g.add_stock(10, 5, date(2999, 12, 31), None)
    g.add_stock(10, 5, today + timedelta(days=30), None)
    _plan(g, MON - timedelta(days=7), "Dinner")
    _plan(g, MON - timedelta(days=30), "Dinner")
    ctx = build_context(g, MON)
    [r] = ctx["recipes"]
    assert r == {"id": 1, "name": "Bread", "ingredients": ["Flour"], "fulfillment": 0.0}  # 115 g in stock < 150 g
    assert ctx["expiring"] == [{"product": "Flour", "best_before_date": (today + timedelta(days=2)).isoformat()}]
    assert ctx["recent_recipes"] == ["Bread"]
    assert len(ctx["open_slots"]) == 9 and ctx["open_slots"][0] == [MON.isoformat(), "Dinner"]
    g.add_stock(10, 50, None, None)
    assert build_context(g, MON)["recipes"][0]["fulfillment"] == 1.0
