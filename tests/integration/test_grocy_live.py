"""Live Grocy checks. Skipped unless GROCY_URL and GROCY_API_KEY are set.

GROCY_URL=http://<host>:9283/api GROCY_API_KEY=... uv run pytest tests/integration -v
Everything created here is deleted afterwards; names carry a uuid suffix so reruns never collide.
"""
import os
import uuid
from datetime import date, timedelta

import pytest

from app.barcode import relink
from app.grocy import Grocy
from app.setup_grocy import bootstrap, ids

pytestmark = pytest.mark.skipif(not (os.environ.get("GROCY_URL") and os.environ.get("GROCY_API_KEY")),
                                reason="needs GROCY_URL and GROCY_API_KEY (live Grocy)")


@pytest.fixture(scope="module")
def grocy():
    return Grocy(os.environ["GROCY_URL"], os.environ["GROCY_API_KEY"])


@pytest.fixture(scope="module")
def i(grocy):
    bootstrap(grocy)
    bootstrap(grocy)  # idempotent
    return ids(grocy)


@pytest.fixture
def made(grocy):
    """Register (entity, id) for cleanup. Deleted in reverse order, so register parents before children
    (or insert(0, ...)). Failures are collected and fail the test, so leftovers are never silent."""
    rows: list[tuple[str, int]] = []
    yield rows
    left = []
    for entity, id in reversed(rows):
        try:
            if entity == "products":
                try:
                    grocy.set_amount(id, 0)  # best effort: clear stock so the delete is not blocked
                except Exception:
                    pass
            grocy.delete(entity, id)
        except Exception as e:
            left.append(f"{entity}/{id}: {e}")
    if left:
        pytest.fail("test data left behind in Grocy (delete zz-test-* by hand): " + "; ".join(left))


def _product(grocy, i, made, tag, **extra):
    pid = grocy.create("products", {
        "name": f"zz-test-{tag}-{uuid.uuid4().hex[:8]}", "location_id": i.location["Pantry"],
        "qu_id_stock": i.unit["g"], "qu_id_purchase": i.unit["g"], "qu_id_consume": i.unit["g"],
        "qu_id_price": i.unit["g"], **extra})
    made.append(("products", pid))
    return pid


def test_bootstrap_created_everything(i):
    assert {"g", "ml", "piece"} <= set(i.unit)
    assert {"Fridge", "Pantry"} <= set(i.location)
    assert {"Lunch", "Dinner"} <= set(i.section)
    assert {"Meal plan", "Extras"} <= set(i.list)


def test_global_conversions_present(grocy, i):  # (f)
    have = {(int(c["from_qu_id"]), int(c["to_qu_id"])): float(c["factor"])
            for c in grocy.objects("quantity_unit_conversions") if c.get("product_id") is None}
    assert have[(i.unit["kg"], i.unit["g"])] == 1000
    assert have[(i.unit["cup"], i.unit["ml"])] == pytest.approx(236.6)


def test_stock_add_consume_amounts_and_best_before(grocy, i, made):  # (g)
    pid = _product(grocy, i, made, "stock")
    bb = date.today() + timedelta(days=5)
    grocy.add_stock(pid, 500, bb, i.location["Pantry"])
    assert grocy.stock_amounts()[pid] == pytest.approx(500)
    entries = [e for e in grocy.stock_entries() if int(e["product_id"]) == pid]
    assert entries and date.fromisoformat(str(entries[0]["best_before_date"])[:10]) == bb
    assert isinstance(entries[0]["best_before_date"], str)
    grocy.consume(pid, 200)
    assert grocy.stock_amounts()[pid] == pytest.approx(300)
    assert "due_products" in grocy.volatile(7)


def test_stock_amounts_rolls_children_into_parent(grocy, i, made):
    parent = _product(grocy, i, made, "parent")
    child = _product(grocy, i, made, "child", parent_product_id=parent)
    grocy.add_stock(child, 50, None, i.location["Pantry"])
    assert grocy.stock_amounts()[parent] == pytest.approx(50)


def test_get_object_missing_is_none(grocy):  # (b) Grocy must answer 404, not 400
    assert grocy.get_object("products", 987654321) is None


def test_barcodes_lookup_and_updates(grocy, i, made):  # (c) (d)
    code = str(int(uuid.uuid4().int % 10**12)).zfill(12)
    assert grocy.product_by_barcode(code) is None
    pid = _product(grocy, i, made, "bc")
    bid = grocy.create("product_barcodes", {"product_id": pid, "barcode": code})
    made.append(("product_barcodes", bid))
    hit = grocy.product_by_barcode(code)
    assert int(hit["product"]["id"]) == pid and hit["product"]["name"].startswith("zz-test-bc")
    # (d) PUT on a product_barcodes row
    code2 = str(int(uuid.uuid4().int % 10**12)).zfill(12)
    grocy.update("product_barcodes", bid, {"product_id": pid, "barcode": code2})
    assert grocy.product_by_barcode(code) is None
    assert int(grocy.product_by_barcode(code2)["product"]["id"]) == pid
    # the real relink path: creates/finds a generic and sets parent_product_id on the child
    generic = f"zz-test-generic-{uuid.uuid4().hex[:8]}"
    relink(grocy, pid, generic)
    parent = int(grocy.get_object("products", pid)["parent_product_id"])
    made.insert(0, ("products", parent))  # deleted last: pid still references it
    assert grocy.get_object("products", parent)["name"] == generic


def test_shopping_list_query_filters_by_list(grocy, i, made):  # (a)
    pid = _product(grocy, i, made, "shop")
    lists = []
    for n in ("a", "b"):
        lid = grocy.create("shopping_lists", {"name": f"zz-test-{n}-{uuid.uuid4().hex[:8]}"})
        made.append(("shopping_lists", lid))
        rid = grocy.create("shopping_list", {"shopping_list_id": lid, "product_id": pid, "amount": 1, "done": 0})
        made.append(("shopping_list", rid))
        lists.append((lid, rid))
    for lid, rid in lists:
        rows = grocy.objects("shopping_list", shopping_list_id=lid)
        assert [int(r["id"]) for r in rows] == [rid]


def test_meal_plan_row_done_and_delete(grocy, i, made):  # (e)
    rec = grocy.create("recipes", {"name": f"zz-test-recipe-{uuid.uuid4().hex[:8]}", "base_servings": 2})
    made.append(("recipes", rec))
    row = grocy.create("meal_plan", {"day": date.today().isoformat(), "type": "recipe", "recipe_id": rec,
                                     "recipe_servings": 4, "section_id": i.section["Dinner"],
                                     "note": "", "done": 0})
    made.append(("meal_plan", row))
    assert int(float(grocy.get_object("meal_plan", row)["done"] or 0)) == 0
    grocy.update("meal_plan", row, {"done": 1})
    assert int(float(grocy.get_object("meal_plan", row)["done"])) == 1
    grocy.delete("meal_plan", row)
    assert grocy.get_object("meal_plan", row) is None


def test_userfield_roundtrip(grocy, i, made):
    pid = _product(grocy, i, made, "uf")
    grocy.set_userfields("products", pid, {"kcal_100": 52, "walmart_item_id": "12345"})
    uf = grocy.userfields("products", pid)
    assert float(uf["kcal_100"]) == 52 and uf["walmart_item_id"] == "12345"


def test_chore_execute_consumes_supply_and_moves_date(grocy, i, made):
    pid = _product(grocy, i, made, "supply")
    grocy.add_stock(pid, 4, None, i.location["Pantry"])
    cid = grocy.create("chores", {
        "name": f"zz-test-chore-{uuid.uuid4().hex[:8]}", "period_type": "daily", "period_interval": 1,
        "consume_product_on_chore_execution": 1, "product_id": pid, "product_amount": 1})
    made.append(("chores", cid))  # after the product, so it is deleted first
    grocy.execute_chore(cid, None)
    assert grocy.stock_amounts()[pid] == pytest.approx(3)
    row = next(r for r in grocy.chores_status() if int(r["chore_id"]) == cid)
    assert row["last_tracked_time"] and row["next_estimated_execution_time"] > row["last_tracked_time"]
