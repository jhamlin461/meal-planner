from datetime import date

from app.setup_grocy import ids
from app.shopping import combined_need, rebuild_lists, shopping_window, walmart_items, week_needs

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
    assert combined_need(needs, {10: 500}, {}) == {10: 100}


def test_week_needs_ignores_done_notes_and_coerces_strings():
    entries = [{"type": "recipe", "recipe_id": "1", "recipe_servings": "2", "done": "1"},
               {"type": "note", "recipe_id": 1, "recipe_servings": 2, "done": 0},
               {"type": "recipe", "recipe_id": "1", "recipe_servings": "2", "done": "0"}]
    assert week_needs(entries, {1: [{"product_id": "10", "amount": "100"}]}, {1: 2}) == {10: 100}


def _list(g, name):
    return g.objects("shopping_list", shopping_list_id=ids(g).list[name])


def test_rebuild_removes_unplanned_and_keeps_extras(fake_grocy_with_week):
    g = fake_grocy_with_week
    assert rebuild_lists(g, TODAY, 6) == {10: 200}  # 150*4/2 - 100 stock
    assert [(r["product_id"], r["amount"]) for r in _list(g, "Meal plan")] == [(10, 200)]
    g.delete("meal_plan", g.objects("meal_plan")[0]["id"])
    assert rebuild_lists(g, TODAY, 6) == {}
    assert _list(g, "Meal plan") == []
    assert [r["note"] for r in _list(g, "Extras")] == ["extra"]


def test_rebuild_preserves_done_flags(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_lists(g, TODAY, 6)
    g.update("shopping_list", _list(g, "Meal plan")[0]["id"], {"done": 1})
    rebuild_lists(g, TODAY, 6)
    assert [r["done"] for r in _list(g, "Meal plan")] == [1]


def test_rebuild_skips_missing_recipe(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("meal_plan", {"day": "2026-11-20", "type": "recipe", "recipe_id": 999,
                           "recipe_servings": 4, "done": 0})
    assert rebuild_lists(g, TODAY, 6) == {10: 200}


def test_rebuild_ignores_entries_outside_window(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("meal_plan", {"day": "2026-12-10", "type": "recipe", "recipe_id": 1,
                           "recipe_servings": 4, "done": 0})
    assert rebuild_lists(g, TODAY, 6) == {10: 200}


def test_walmart_items_splits_cart_and_remaining(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.set_userfields("products", 10, {"walmart_item_id": "555", "package_amount": "500"})
    other = g.create("products", {"name": "Salt"})
    assert walmart_items(g, {10: 600, other: 5}) == ([("555", 2)], [{"id": other, "name": "Salt"}])


def test_staple_in_stock_stays_off_list_end_to_end(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.set_userfields("products", 10, {"staple": "1"})  # stock is 100 g, need 300 g
    assert rebuild_lists(g, TODAY, 6) == {}
    assert _list(g, "Meal plan") == []


def test_float_noise_does_not_create_phantom_item():
    assert combined_need({1: 0.1 * 3}, {1: 0.3}, {}) == {}


def test_rebuild_survives_free_text_row_and_bad_base_servings(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.create("shopping_list", {"shopping_list_id": ids(g).list["Meal plan"], "product_id": None,
                               "amount": 1, "done": 1, "note": "free text"})
    g.update("recipes", 1, {"base_servings": ""})
    assert rebuild_lists(g, TODAY, 6) == {10: 500}  # 150*4/1 - 100


def test_walmart_items_skips_missing_product(fake_grocy_with_week):
    assert walmart_items(fake_grocy_with_week, {999: 5}) == ([], [])


def test_rebuild_keeps_row_ids_and_dedupes(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_lists(g, TODAY, 6)
    [row] = _list(g, "Meal plan")
    g.update("shopping_list", row["id"], {"done": 1})
    dup = g.create("shopping_list", {"shopping_list_id": ids(g).list["Meal plan"], "product_id": 10, "amount": 1,
                                     "done": 0})
    rebuild_lists(g, TODAY, 6)
    [again] = _list(g, "Meal plan")
    assert again["id"] == row["id"] and again["done"] == 1 and again["id"] != dup
    assert again["amount"] == 200  # 4 servings at 150 g per 2 = 300 g, minus 100 g in stock


def test_rebuild_unticks_row_when_amount_grows_but_not_when_it_shrinks(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_lists(g, TODAY, 6)
    [row] = _list(g, "Meal plan")  # 200 g short
    g.update("shopping_list", row["id"], {"done": 1})
    g.add_stock(10, 50, None, None)  # shortfall shrinks to 150
    rebuild_lists(g, TODAY, 6)
    assert _list(g, "Meal plan")[0]["done"] == 1 and _list(g, "Meal plan")[0]["amount"] == 150
    g.stock[10].pop()
    g.stock[10][0]["amount"] = 0  # shortfall grows to 300
    rebuild_lists(g, TODAY, 6)
    [after] = _list(g, "Meal plan")
    assert after["id"] == row["id"] and after["amount"] == 300 and after["done"] == 0


def _supply(g, name="Trash bags", min_stock=2, stock=0):
    pid = g.create("products", {"name": name, "min_stock_amount": min_stock})
    if stock:
        g.add_stock(pid, stock, None, None)
    return pid


def _low(g):
    """What the rebuild wants besides the planned flour (product 10)."""
    return {k: v for k, v in rebuild_lists(g, TODAY, 6).items() if k != 10}


def test_running_low_list_rebuilt_in_place(fake_grocy_with_week):
    g = fake_grocy_with_week
    extras = _list(g, "Extras")
    bags = _supply(g, stock=1)
    assert _low(g) == {bags: 1}
    [row] = _list(g, "Running low")
    assert (row["product_id"], row["amount"], row["done"]) == (bags, 1, 0)
    g.update("shopping_list", row["id"], {"done": 1})
    g.consume(bags, 1)  # now 2 short: the tick covered 1 only
    rebuild_lists(g, TODAY, 6)
    [again] = _list(g, "Running low")
    assert (again["id"], again["amount"], again["done"]) == (row["id"], 2, 0)
    g.add_stock(bags, 3, None, None)
    assert _low(g) == {}
    assert _list(g, "Running low") == [] and _list(g, "Extras") == extras
    assert rebuild_lists(g, TODAY, 6) == {10: 200} and _list(g, "Running low") == []


def test_two_brands_count_toward_the_generic_keep_at_least(fake_grocy_with_week):
    g = fake_grocy_with_week
    bags = g.create("products", {"name": "Trash bags", "min_stock_amount": 2,
                                 "cumulate_min_stock_amount_of_sub_products": 1})
    glad = g.create("products", {"name": "Glad bags", "parent_product_id": bags})
    hefty = g.create("products", {"name": "Hefty bags", "parent_product_id": bags})
    g.add_stock(glad, 1, None, None)
    g.add_stock(hefty, 1, None, None)
    assert _low(g) == {}  # one of each brand: 2 in all
    g.consume(bags, 2)  # used from the generic: Grocy takes it from the brands
    assert g.stock_amounts()[bags] == 0
    assert _low(g) == {bags: 2}
    assert [r["product_id"] for r in _list(g, "Running low")] == [bags]


def test_running_low_rebuild_leaves_meal_plan_rows_alone(fake_grocy_with_week):
    g = fake_grocy_with_week
    rebuild_lists(g, TODAY, 6)
    [row] = _list(g, "Meal plan")
    g.update("shopping_list", row["id"], {"done": 1})
    _supply(g, stock=1)
    rebuild_lists(g, TODAY, 6)
    assert _list(g, "Meal plan") == [{**row, "done": 1}]


def test_trash_chore_drives_running_low(fake_grocy_with_week):
    g = fake_grocy_with_week
    bags = _supply(g, stock=3)
    chore = g.create("chores", {"name": "Take out the trash", "period_type": "weekly", "period_config": "sunday",
                                "consume_product_on_execution": 1, "product_id": bags, "product_amount": 1})
    g.execute_chore(chore, None)  # 2 left: not below keep-at-least
    assert _low(g) == {} and _list(g, "Running low") == []
    g.execute_chore(chore, None)  # 1 left
    rebuild_lists(g, TODAY, 6)
    assert [(r["product_id"], r["amount"]) for r in _list(g, "Running low")] == [(bags, 1)]


def test_fake_volatile_keeps_fixed_result_without_min_stock(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.volatile_result["due_products"] = [{"product_id": 10}]
    assert g.volatile(3)["due_products"] == [{"product_id": 10}] and g.volatile(3)["missing_products"] == []
    bags = _supply(g)
    v = g.volatile(3)
    assert v["due_products"] == [{"product_id": 10}]
    assert v["missing_products"] == [{"id": bags, "name": "Trash bags", "amount_missing": 2, "is_partly_in_stock": 0}]


def test_combined_need_uses_only_the_stock_it_is_given(fake_grocy_with_week):
    g = fake_grocy_with_week
    bags = _supply(g, stock=5)  # Grocy's total is 5, above the minimum of 2
    assert rebuild_lists(g, TODAY, 6).get(bags) is None
    assert combined_need({}, {bags: 1}, {bags: 2}) == {bags: 1}  # free stock 1: under the minimum


def test_staple_listed_only_when_out(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.set_userfields("products", 10, {"staple": "1"})  # needs 300 g
    assert rebuild_lists(g, TODAY, 6) == {}  # 100 g in stock
    g.stock[10][0]["amount"] = 0
    assert rebuild_lists(g, TODAY, 6) == {10: 300}


def test_plan_covered_but_not_plan_plus_minimum_lists_the_difference(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.add_stock(10, 250, None, None)  # 350 g in stock, plan needs 300
    g.update("products", 10, {"min_stock_amount": 100})
    assert rebuild_lists(g, TODAY, 6) == {10: 50}
    assert [(r["product_id"], r["amount"]) for r in _list(g, "Meal plan")] == [(10, 50)]
    assert _list(g, "Running low") == []


def test_tick_follows_a_product_moving_between_lists(fake_grocy_with_week):
    g = fake_grocy_with_week
    g.update("products", 10, {"min_stock_amount": 400})  # below its minimum and planned: Meal plan, 600
    rebuild_lists(g, TODAY, 6)
    [row] = _list(g, "Meal plan")
    g.update("shopping_list", row["id"], {"done": 1})
    g.delete("meal_plan", g.objects("meal_plan")[0]["id"])  # plan gone: now only Running low, 300
    rebuild_lists(g, TODAY, 6)
    assert _list(g, "Meal plan") == []
    [moved] = _list(g, "Running low")
    assert (moved["product_id"], moved["amount"], moved["done"]) == (10, 300, 1)
