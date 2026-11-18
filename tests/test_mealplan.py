from datetime import date
from types import SimpleNamespace

from app.mealplan import LEFTOVER_PREFIX, cook, default_servings, plan_meal, unplan_meal, week_view
from app.setup_grocy import ids

TODAY = date(2026, 11, 18)
SETTINGS = SimpleNamespace(shopping_weekday=6)


def _g(fake):
    fake.entities["recipes"][1]["name"] = "Brownies"
    for r in fake.objects("meal_plan"):
        fake.delete("meal_plan", r["id"])
    return fake


def _list(g):
    return g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])


def test_default_servings():
    assert default_servings(date(2026, 11, 22), "Dinner") == 4
    assert default_servings(date(2026, 11, 20), "Dinner") == 2
    assert default_servings(date(2026, 11, 21), "Lunch") == 2


def test_plan_dinner_adds_leftover_lunch_next_day(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    eid = plan_meal(g, date(2026, 11, 18), "Dinner", 1, None, SETTINGS, today=TODAY)
    assert g.get_object("meal_plan", eid)["recipe_servings"] == 4
    [note] = g.objects("meal_plan", type="note")
    assert note["day"] == "2026-11-19" and note["section_id"] == ids(g).section["Lunch"]
    assert note["note"] == LEFTOVER_PREFIX + "Brownies"
    assert _list(g)  # rebuilt: 300 g needed, 100 g in stock


def test_friday_dinner_no_leftover(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    eid = plan_meal(g, date(2026, 11, 20), "Dinner", 1, None, SETTINGS, today=TODAY)
    assert g.get_object("meal_plan", eid)["recipe_servings"] == 2
    assert g.objects("meal_plan", type="note") == []


def test_unplan_removes_leftover_and_list_items(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    eid = plan_meal(g, date(2026, 11, 18), "Dinner", 1, None, SETTINGS, today=TODAY)
    unplan_meal(g, eid, SETTINGS, today=TODAY)
    assert g.objects("meal_plan") == [] and _list(g) == []


def test_cook_deducts_scaled_amounts_and_reports_short(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    eid = plan_meal(g, date(2026, 11, 18), "Dinner", 1, 4, SETTINGS, today=TODAY)
    assert cook(g, eid) == ["Flour"]  # need 300, stock 100
    assert g.stock_amounts().get(10, 0) == 0
    assert int(g.get_object("meal_plan", eid)["done"]) == 1


def test_cook_twice_is_noop(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    eid = plan_meal(g, date(2026, 11, 20), "Dinner", 1, 2, SETTINGS, today=TODAY)  # need 150
    assert cook(g, eid) == ["Flour"]
    g.add_stock(10, 50, None, None)
    assert cook(g, eid) == [] and g.stock_amounts()[10] == 50


def test_week_view_groups_by_day_and_section(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    plan_meal(g, date(2026, 11, 18), "Dinner", 1, None, SETTINGS, today=TODAY)
    days = week_view(g, date(2026, 11, 16))
    assert len(days) == 7 and days[0].day == date(2026, 11, 16)
    assert len(days[2].dinner) == 1 and len(days[3].lunch) == 1 and days[3].dinner == []


def test_cook_note_is_noop(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    plan_meal(g, date(2026, 11, 18), "Dinner", 1, None, SETTINGS, today=TODAY)
    [note] = g.objects("meal_plan", type="note")
    assert cook(g, note["id"]) == []
    assert g.stock_amounts()[10] == 100 and int(g.get_object("meal_plan", note["id"])["done"]) == 0


def test_cook_repeated_ingredient_consumes_once_and_reports_once(fake_grocy_with_week):
    g = _g(fake_grocy_with_week)
    g.create("recipes_pos", {"recipe_id": 1, "product_id": 10, "amount": 150, "note": ""})
    eid = plan_meal(g, date(2026, 11, 20), "Dinner", 1, 2, SETTINGS, today=TODAY)  # need 300, stock 100
    assert cook(g, eid) == ["Flour"]
    assert g.stock_amounts().get(10, 0) == 0
