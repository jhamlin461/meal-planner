import httpx
import pytest

from app.nutrition import fdc_lookup, fill_missing_nutrition, recipe_macros
from tests.fakes import FakeGrocy

INFO = {
    1: {"name": "Flour", "g_per_unit": "1", "kcal_100": "364", "protein_100": 10, "carbs_100": 76, "fat_100": 1},
    2: {"name": "Egg", "g_per_unit": 50, "kcal_100": 143, "protein_100": 12.6, "carbs_100": 0.7, "fat_100": 9.5},
    3: {"name": "Mystery", "g_per_unit": 1, "kcal_100": "", "protein_100": None, "carbs_100": None, "fat_100": None},
    4: {"name": "Piece thing", "g_per_unit": None, "kcal_100": 100, "protein_100": 1, "carbs_100": 1, "fat_100": 1},
}


def test_recipe_macros_per_serving():
    m = recipe_macros([{"product_id": 1, "amount": "100"}, {"product_id": 2, "amount": 2}], INFO, 2)
    assert m.kcal == pytest.approx(253.5)
    assert m.protein == pytest.approx((10 + 12.6) / 2)
    assert m.missing == []


def test_missing_nutrition_listed():
    pos = [{"product_id": 3, "amount": 5}, {"product_id": 4, "amount": 1}, {"product_id": 3, "amount": 1},
           {"product_id": 1, "amount": 100}]
    m = recipe_macros(pos, INFO, 0)  # 0 servings -> 1
    assert m.missing == ["Mystery", "Piece thing"]
    assert m.kcal == pytest.approx(364)


FDC = {"foods": [{"foodNutrients": [
    {"nutrientNumber": "1008", "value": 364}, {"nutrientNumber": "1003", "value": 10.3},
    {"nutrientNumber": "1005", "value": 76.3}, {"nutrientNumber": "1004", "value": 1.0},
    {"nutrientNumber": "9999", "value": 5}]}]}


def _http(status=200, body=FDC, seen=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fdc_lookup_parses_nutrients():
    seen = []
    out = fdc_lookup("flour", "KEY", _http(seen=seen))
    assert out == {"kcal_100": 364, "protein_100": 10.3, "carbs_100": 76.3, "fat_100": 1.0}
    assert seen[0].url.params["query"] == "flour" and seen[0].url.params["api_key"] == "KEY"
    assert seen[0].headers["user-agent"] == "MealPlanner/1.0"


def test_fdc_lookup_failures_return_none():
    assert fdc_lookup("x", "K", _http(500)) is None
    assert fdc_lookup("x", "K", _http(body={"foods": []})) is None

    def boom(req):
        raise httpx.ConnectError("down")
    assert fdc_lookup("x", "K", httpx.Client(transport=httpx.MockTransport(boom))) is None


def test_fill_missing_nutrition():
    g = FakeGrocy()
    g.entities["products"] = {5: {"id": 5, "name": "Flour"}, 6: {"id": 6, "name": "Rice"}}
    g.userfield_data[("products", 6)] = {"kcal_100": "130"}
    assert fill_missing_nutrition(g, 5, "K", _http()) is True
    assert g.userfields("products", 5)["kcal_100"] == 364
    assert fill_missing_nutrition(g, 6, "K", _http()) is False
    assert g.userfields("products", 6) == {"kcal_100": "130"}
    g.entities["products"][7] = {"id": 7, "name": "Oats"}
    assert fill_missing_nutrition(g, 7, "K", _http(500)) is False
    assert g.userfields("products", 7) == {}
