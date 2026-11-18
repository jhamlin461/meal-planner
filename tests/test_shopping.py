from datetime import date

from app.setup_grocy import ids
from app.shopping import (rebuild_meal_plan_list, shopping_window, shortfall, walmart_items,
                          week_needs)

TODAY = date(2026, 11, 18)


def test_shopping_window_wednesday_sunday_shop():
    assert shopping_window(date(2026, 11, 18), 6) == (date(2026, 11, 18), date(2026, 11, 29))
    assert shopping_window(date(2026, 11, 22), 6) == (date(2026, 11, 22), date(2026, 11, 29))


def test_two_recipes_sharing_flour_sum_before_subtracting():
    entries = [{"type": "recipe", "recipe_id": 1, "recipe_servings": 4, "done": 0},
               {"type": "recipe", "recipe_id": 2, "recipe_servings": 2, "done": 0}]
    positions = {1: [{"product_id": 10, "amount": 150}], 2: [{"product_id": 10, "amount": 300}]}
    needs = week_needs(entries, positions, {1: 2, 2: 2})
    assert needs == {10: 600}
    assert shortfall(needs, {10: 500}, set()) == {10: 100}


def test_week_needs_ignores_done_notes_and_coerces_strings():
    entries = [{"type": "recipe", "recipe_id": "1", "recipe_servings": "2", "done": "1"},
               {"type": "note", "recipe_id": 1, "recipe_servings": 2, "done": 0},
               {"type": "recipe", "recipe_id": "1", "recipe_servings": "2", "done": "0"}]
    assert week_needs(entries, {1: [{"product_id": "10", "amount": "100"}]}, {1: 2}) == {10: 100}


def test_staple_only_when_out():
    assert shortfall({5: 2}, {5: 1}, {5}) == {} and shortfall({5: 2}, {5: 0}, {5}) == {5: 2}


def _list(g, name):
    return g.objects("shopping_list", shopping_list_id=ids(g).list[name])


def test_rebuild_removes_unplanned_and_keeps_extras(fake_grocy_with_week):
    g = fake_grocy_with_week
    assert rebuild_meal_plan_list(g, TODAY, 6) == {10: 200}  # 150*4/2 - 100 stock
    assert [(r["product_id"], r["amount"]) for r in _list(g, "Meal plan")] == [(10, 200)]
    g.delete("meal_plan", g.objects("meal_plan")[0]["id"])
    assert rebuild_meal_plan_list(g, TODAY, 6) == {}
    assert _list(g, "Meal plan") == []
    assert [r["note"] for r in _list(g, "Extras")] == ["extra"]


def test_rebuild_preserves_done_flags(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_meal_plan_list(g, TODAY, 6)
    g.update("shopping_list", _list(g, "Meal plan")[0]["id"], {"done": 1})
    rebuild_meal_plan_list(g, TODAY, 6)
    assert [r["done"] for r in _list(g, "Meal plan")] == [1]


def test_rebuild_skips_missing_recipe(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("meal_plan", {"day": "2026-11-20", "type": "recipe", "recipe_id": 999,
                           "recipe_servings": 4, "done": 0})
    assert rebuild_meal_plan_list(g, TODAY, 6) == {10: 200}


def test_rebuild_ignores_entries_outside_window(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("meal_plan", {"day": "2026-12-10", "type": "recipe", "recipe_id": 1,
                           "recipe_servings": 4, "done": 0})
    assert rebuild_meal_plan_list(g, TODAY, 6) == {10: 200}


def test_walmart_items_splits_cart_and_remaining(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.set_userfields("products", 10, {"walmart_item_id": "555", "package_amount": "500"})
    other = g.create("products", {"name": "Salt"})
    assert walmart_items(g, {10: 600, other: 5}) == ([("555", 2)], [{"id": other, "name": "Salt"}])


def test_staple_in_stock_stays_off_list_end_to_end(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.set_userfields("products", 10, {"staple": "1"})  # stock is 100 g, need 300 g
    assert rebuild_meal_plan_list(g, TODAY, 6) == {}
    assert _list(g, "Meal plan") == []


def test_float_noise_does_not_create_phantom_item():
    assert shortfall({1: 0.1 * 3}, {1: 0.3}, set()) == {}


def test_rebuild_survives_free_text_row_and_bad_base_servings(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("shopping_list", {"shopping_list_id": ids(g).list["Meal plan"], "product_id": None,
                               "amount": 1, "done": 1, "note": "free text"})
    g.update("recipes", 1, {"base_servings": ""})
    assert rebuild_meal_plan_list(g, TODAY, 6) == {10: 500}  # 150*4/1 - 100


def test_walmart_items_skips_missing_product(fake_grocy_with_week):
    assert walmart_items(fake_grocy_with_week, {999: 5}) == ([], [])


def test_rebuild_keeps_row_ids_and_dedupes(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_meal_plan_list(g, TODAY, 6)
    [row] = _list(g, "Meal plan")
    g.update("shopping_list", row["id"], {"done": 1})
    dup = g.create("shopping_list", {"shopping_list_id": ids(g).list["Meal plan"], "product_id": 10, "amount": 1,
                                     "done": 0})
    rebuild_meal_plan_list(g, TODAY, 6)
    [again] = _list(g, "Meal plan")
    assert again["id"] == row["id"] and again["done"] == 1 and again["id"] != dup
    assert again["amount"] == 200  # 4 servings at 150 g per 2 = 300 g, minus 100 g in stock


def test_rebuild_unticks_row_when_amount_grows_but_not_when_it_shrinks(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_meal_plan_list(g, TODAY, 6)
    [row] = _list(g, "Meal plan")  # 200 g short
    g.update("shopping_list", row["id"], {"done": 1})
    g.add_stock(10, 50, None, None)  # shortfall shrinks to 150
    rebuild_meal_plan_list(g, TODAY, 6)
    assert _list(g, "Meal plan")[0]["done"] == 1 and _list(g, "Meal plan")[0]["amount"] == 150
    g.stock[10].pop()
    g.stock[10][0]["amount"] = 0  # shortfall grows to 300
    rebuild_meal_plan_list(g, TODAY, 6)
    [after] = _list(g, "Meal plan")
    assert after["id"] == row["id"] and after["amount"] == 300 and after["done"] == 0
