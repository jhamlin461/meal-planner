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
    assert r == {"id": 1, "name": "Bread", "ingredients": ["Flour"], "fulfillment": 0.0,
                 "fully_stocked": False, "kit": False}  # 115 g in stock < 150 g
    assert ctx["expiring"] == [{"product": "Flour", "best_before_date": (today + timedelta(days=2)).isoformat()}]
    assert ctx["recent_recipes"] == ["Bread"]
    assert len(ctx["open_slots"]) == 9 and ctx["open_slots"][0] == [MON.isoformat(), "Dinner"]
    g.add_stock(10, 50, None, None)
    assert build_context(g, MON)["recipes"][0]["fulfillment"] == 1.0


def test_grocy_internal_meal_plan_recipes_are_never_suggested(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    shadow = g.create("recipes", {"name": "2026-11-16#1", "type": "mealplan-shadow", "base_servings": 1})
    g.create("recipes", {"name": "2026-11-16", "type": "mealplan-day", "base_servings": 1})
    assert [r["id"] for r in build_context(g, MON)["recipes"]] == [1]
    week = WeekSuggestions(items=[Suggestion(day=MON.isoformat(), section="Dinner", recipe_id=shadow, reason="x")])
    assert suggest(g, FakeAI(week=week), MON) == []


# ---- Task 8: cook from what we have ---------------------------------------------------------------------------------
def _recipe(g, name, wants, base=2):
    """A recipe whose positions are (product id, amount) pairs; amount 0 is a 'to taste' marker."""
    rid = g.create("recipes", {"name": name, "base_servings": base})
    for pid, amount in wants:
        g.create("recipes_pos", {"recipe_id": rid, "product_id": pid, "amount": amount,
                                 "qu_id": ids(g).unit["g"], "note": ""})
    return rid


def _by_name(ctx):
    return {r["name"]: r for r in ctx["recipes"]}


def test_fulfillment_scales_to_servings_and_ignores_unknown_amounts(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    g.entities["products"][11] = {"id": 11, "name": "Salt"}
    _recipe(g, "Salty", [(10, 150), (11, 0)], base=2)  # 100 g flour in stock; salt has no amount
    assert _by_name(build_context(g, MON))["Salty"]["fulfillment"] == 0.0  # 150 g needed at base
    assert _by_name(build_context(g, MON, servings=1))["Salty"]["fulfillment"] == 1.0  # 75 g; salt is not counted
    r = _by_name(build_context(g, MON, servings=1))["Salty"]
    assert r["fully_stocked"] is True
    assert _by_name(build_context(g, MON, servings=4))["Salty"]["fully_stocked"] is False  # 300 g


def test_local_picks_from_stock_order():
    from app.suggest import local_picks
    slots = [[(MON + timedelta(days=i)).isoformat(), "Dinner"] for i in range(4)]

    def r(i, name, full, fulfil, ings=("x",), kit=False):
        return {"id": i, "name": name, "ingredients": list(ings), "fulfillment": fulfil, "fully_stocked": full, "kit": kit}

    ctx = {"recipes": [r(1, "Half", False, 0.5), r(2, "Recent full", True, 1.0), r(3, "Full", True, 1.0),
                       r(4, "Kit", True, 1.0, kit=True)],
           "expiring": [], "recent_recipes": ["Recent full"], "open_slots": slots}
    picks = local_picks(ctx, from_stock=True)
    assert [p.recipe_id for p in picks] == [4, 3, 2, 1]
    assert [p.reason for p in picks] == ["Your meal kit is here", "You have everything", "You have everything",
                                         "You have most of it"]
    assert [p.recipe_id for p in local_picks(ctx)][:1] != [4]  # without the flag the order is the old one


def test_from_stock_reasons_are_true_for_every_pick():
    from app.suggest import local_picks
    slots = [[(MON + timedelta(days=i)).isoformat(), "Dinner"] for i in range(4)]

    def r(i, name, fulfil, ings=("x",)):
        return {"id": i, "name": name, "ingredients": list(ings), "fulfillment": fulfil, "fully_stocked": False,
                "kit": False}

    ctx = {"recipes": [r(1, "Most", 0.75), r(2, "Nothing", 0.0), r(3, "Old rice", 0.0, ("Rice",)), r(4, "Some", 0.25)],
           "expiring": [{"product": "Rice", "best_before_date": "2026-11-20"}], "recent_recipes": ["Nothing"],
           "open_slots": slots}
    got = {p.recipe_id: p.reason for p in local_picks(ctx, from_stock=True)}
    assert got == {1: "You have most of it", 2: "Nothing for this in stock yet", 3: "Uses Rice before it expires",
                   4: "You have some of it"}


def test_from_stock_prompt_flag():
    from types import SimpleNamespace as NS
    from app import ai as ai_mod
    calls = []
    c = NS(beta=NS(messages=NS(parse=lambda **kw: calls.append(kw) or NS(stop_reason="end_turn", parsed_output=1))))
    a = ai_mod.AI(c)
    a.suggest_week({})
    a.suggest_week({}, from_stock=True)
    assert calls[0]["system"][0]["text"] == ai_mod.WEEK_PROMPT
    text = calls[1]["system"][0]["text"]
    assert text == ai_mod.STOCK_WEEK_PROMPT and "fully stocked" in text and "3 weeks" in text


def test_suggest_from_stock_asks_for_stock_picks_and_free_mode_ranks_locally(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    g.entities["products"][11] = {"id": 11, "name": "Rice"}
    low = _recipe(g, "Needs rice", [(11, 500)])
    full = _recipe(g, "Light bread", [(10, 50)])
    seen = []
    ai = FakeAI(week=WeekSuggestions(items=[]))
    ai.suggest_week = lambda ctx, from_stock=False: seen.append(from_stock) or WeekSuggestions(items=[])
    suggest(g, ai, MON, from_stock=True)
    assert seen == [True]
    picks = suggest(g, FakeAI(mode="free"), MON, from_stock=True)
    assert picks[0].recipe_id == full
    assert picks[0].reason == "You have everything" and low in [p.recipe_id for p in picks]


def test_a_kit_makes_its_recipe_fully_stocked_and_first_and_only_its_own(fake_grocy_with_week, tmp_path):
    from app.house import HouseStore
    from app.suggest import local_picks
    g = _empty(fake_grocy_with_week)
    house = HouseStore(tmp_path / "h.db")
    g.entities["products"][11] = {"id": 11, "name": "Rice"}
    a = _recipe(g, "Rice bowl", [(11, 200)])
    b = _recipe(g, "Rice soup", [(11, 200)])
    _, entries = g.add_stock_entries(11, 200, None, None)
    house.add_kit(a, 11, "t1", entries)
    ctx = build_context(g, MON, house=house)
    got = _by_name(ctx)
    assert got["Rice bowl"]["fully_stocked"] and got["Rice bowl"]["kit"] and got["Rice bowl"]["fulfillment"] == 1.0
    assert not got["Rice soup"]["fully_stocked"] and not got["Rice soup"]["kit"]  # another recipe's kit never counts
    first = local_picks(ctx, from_stock=True)[0]
    assert first.recipe_id == a and first.reason == "Your meal kit is here"


def _grocy_reads(g, first, n):
    """Add n recipes (R<first>..) each with a product of its own, then build a context (Grocy sending each row's
    userfields with the list) and count the Grocy reads."""
    for i in range(first, first + n):
        g.entities["products"][200 + i] = {"id": 200 + i, "name": f"P{i}"}
        _recipe(g, f"R{i}", [(200 + i, 5), (10, 5)])
    plain, uf = g.objects, g.userfields
    calls = []

    def with_userfields(entity, **where):
        calls.append(entity)
        return [{**r, "userfields": uf(entity, r["id"])} if entity in ("products", "recipes") else r
                for r in plain(entity, **where)]

    g.objects = with_userfields
    for name in ("userfields", "stock_amounts", "stock_entries", "get_object"):
        real = getattr(g, name)
        setattr(g, name, lambda *a, _real=real, _n=name, **k: calls.append(_n) or _real(*a, **k))
    ctx = build_context(g, MON, servings=2)
    g.objects, g.userfields = plain, uf
    return ctx, len(calls)


def test_build_context_reads_grocy_a_constant_number_of_times(fake_grocy_with_week):
    g = _empty(fake_grocy_with_week)
    few, n_few = _grocy_reads(g, 0, 2)
    assert [r["name"] for r in few["recipes"]] == ["Bread", "R0", "R1"]
    many, n_many = _grocy_reads(g, 2, 10)  # 4x the recipes, a product each: the same reads
    assert len(many["recipes"]) == 13 and n_many == n_few
