import json
from pathlib import Path

import httpx

from app.ai import ProductGuess
from app.barcode import relink, resolve, resolve_from_photo
from app.setup_grocy import bootstrap
from tests.fakes import FakeAI, FakeGrocy

OFF = json.loads((Path(__file__).parent / "fixtures" / "off_flour.json").read_text())
CODE = "0078742000001"


def http(off=None, upc=None):
    def handler(req):
        if "openfoodfacts" in req.url.host and off is not None:
            return httpx.Response(200, json=off)
        if "upcitemdb" in req.url.host and upc is not None:
            return httpx.Response(200, json=upc)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def guess(**kw):
    d = dict(generic_name="All-purpose flour", is_new_generic=True, stock_unit="g", package_amount=2268,
             location="Pantry", best_before_days=365, g_per_unit=1)
    return ProductGuess.model_construct(**{**d, **kw})  # construct: allows g_per_unit=None


def grocy():
    g = FakeGrocy()
    bootstrap(g)
    return g


def test_known_barcode_returns_existing():
    g = grocy()
    pid = g.create("products", {"name": "Flour"})
    g.create("product_barcodes", {"product_id": pid, "barcode": CODE})
    r = resolve(g, FakeAI(), http(), CODE)
    assert (r.product_id, r.name, r.created) == (pid, "Flour", False)


def test_resolve_creates_parent_child_and_barcode():
    g = grocy()
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    parent = next(p for p in g.objects("products") if p["name"] == "All-purpose flour")
    child = g.get_object("products", r.product_id)
    assert r.created and r.generic_name == "All-purpose flour"
    assert child["name"] == "Great Value All Purpose Flour"
    assert child["parent_product_id"] == parent["id"] and "parent_product_id" not in parent
    assert child["qu_id_stock"] == parent["qu_id_stock"] and child["default_best_before_days"] == 365
    assert g.product_by_barcode(CODE)["product"]["id"] == child["id"]
    assert float(g.userfields("products", parent["id"])["kcal_100"]) == 364
    assert float(g.userfields("products", child["id"])["protein_100"]) == 10.3
    assert float(g.userfields("products", child["id"])["package_amount"]) == 2268


def test_second_scan_reuses_parent_and_keeps_its_nutrition():
    g = grocy()
    resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    other = {"status": 1, "product": {"product_name": "King Arthur Flour", "nutriments": {"energy-kcal_100g": 400}}}
    resolve(g, FakeAI(guess=guess(generic_name="all-purpose FLOUR", is_new_generic=False)), http(off=other), "999")
    parents = [p for p in g.objects("products") if not p.get("parent_product_id")]
    assert len(parents) == 1
    assert float(g.userfields("products", parents[0]["id"])["kcal_100"]) == 364


def test_resolve_off_without_quantity():
    g = grocy()
    off = {"status": 1, "product": {"product_name": "Mystery Flour"}}
    r = resolve(g, FakeAI(guess=guess(package_amount=None)), http(off=off), CODE)
    uf = g.userfields("products", r.product_id)
    assert float(uf["package_amount"]) == 1
    assert "kcal_100" not in uf


def test_bad_location_falls_back_to_pantry():
    g = grocy()
    r = resolve(g, FakeAI(guess=guess(location="Garage")), http(off=OFF), CODE)
    pantry = next(x["id"] for x in g.objects("locations") if x["name"] == "Pantry")
    assert g.get_object("products", r.product_id)["location_id"] == pantry


def test_upc_fallback():
    g = grocy()
    upc = {"items": [{"title": "Bob's Flour", "brand": "Bob's"}]}
    r = resolve(g, FakeAI(guess=guess()), http(upc=upc), CODE)
    assert r.name == "Bob's Flour"


def test_unknown_everywhere_returns_none():
    assert resolve(grocy(), FakeAI(guess=guess()), http(), CODE) is None


def test_network_error_is_not_found():
    def boom(req):
        raise httpx.ConnectError("down")
    assert resolve(grocy(), FakeAI(guess=guess()), httpx.Client(transport=httpx.MockTransport(boom)), CODE) is None


def test_resolve_from_photo():
    g = grocy()
    r = resolve_from_photo(g, FakeAI(guess=guess()), http(), CODE, b"img")
    assert r.name == "All-purpose flour (scanned)" and r.created
    assert g.product_by_barcode(CODE)["product"]["id"] == r.product_id


def test_relink_moves_child():
    g = grocy()
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    relink(g, r.product_id, "Bread flour")
    new = next(p for p in g.objects("products") if p["name"] == "Bread flour")
    assert g.get_object("products", r.product_id)["parent_product_id"] == new["id"]
    assert new["qu_id_stock"] == g.get_object("products", r.product_id)["qu_id_stock"]


def test_duplicate_child_name_gets_barcode_suffix():
    g = grocy()
    resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), "555")
    assert r.name == "Great Value All Purpose Flour (555)"


def test_off_without_name_falls_through_to_upc():
    g = grocy()
    off = {"status": 1, "product": {"nutriments": {"energy-kcal_100g": 350}}}
    upc = {"items": [{"title": "Bob's Flour", "brand": "Bob's"}]}
    r = resolve(g, FakeAI(guess=guess()), http(off=off, upc=upc), CODE)
    assert r.name == "Bob's Flour"
    assert float(g.userfields("products", r.product_id)["kcal_100"]) == 350


def test_zero_parent_nutrition_not_overwritten():
    g = grocy()
    resolve(g, FakeAI(guess=guess()), http(off={"status": 1, "product": {
        "product_name": "A", "nutriments": {"energy-kcal_100g": 0}}}), "1")
    resolve(g, FakeAI(guess=guess(is_new_generic=False)), http(off=OFF), "2")
    parent = next(p for p in g.objects("products") if not p.get("parent_product_id"))
    assert float(g.userfields("products", parent["id"])["kcal_100"]) == 0


def test_g_per_unit_none_defaults():
    g = grocy()
    r = resolve(g, FakeAI(guess=guess(g_per_unit=None)), http(off=OFF), CODE)
    assert float(g.userfields("products", r.product_id)["g_per_unit"]) == 1
    r2 = resolve_from_photo(g, FakeAI(guess=guess(generic_name="Eggs", stock_unit="piece", g_per_unit=None)),
                            http(), "7", b"x")
    assert "g_per_unit" not in g.userfields("products", r2.product_id)


def test_non_dict_json_is_not_found():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1])))
    assert resolve(grocy(), FakeAI(guess=guess()), c, CODE) is None


def test_user_agent_sent():
    seen = []
    def handler(req):
        seen.append(req.headers["user-agent"])
        return httpx.Response(404)
    resolve(grocy(), FakeAI(guess=guess()), httpx.Client(transport=httpx.MockTransport(handler)), CODE)
    assert seen and set(seen) == {"MealPlanner/1.0"}


def test_relink_to_child_name_raises():
    import pytest
    g = grocy()
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    r2 = resolve(g, FakeAI(guess=guess()), http(off=OFF), "555")
    with pytest.raises(ValueError, match="child"):
        relink(g, r2.product_id, "Great Value All Purpose Flour")


def tortillas(g, **uf):
    """An existing generic stocked in pieces, created the way the app creates products."""
    from app.setup_grocy import ids
    piece = ids(g).unit["piece"]
    pid = g.create("products", {"name": "Tortillas", "qu_id_stock": piece, "qu_id_purchase": piece,
                                "qu_id_consume": piece, "qu_id_price": piece})
    g.set_userfields("products", pid, uf)
    return pid, piece


def test_child_of_existing_generic_takes_parent_units():
    g = grocy()
    parent, piece = tortillas(g, g_per_unit=40)
    off = {"status": 1, "product": {"product_name": "Mission Tortillas"}}
    r = resolve(g, FakeAI(guess=guess(generic_name="tortillas", stock_unit="g", package_amount=567)),
                http(off=off), CODE)
    child = g.get_object("products", r.product_id)
    assert child["parent_product_id"] == parent
    assert all(child[k] == piece for k in ("qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price"))
    uf = g.userfields("products", r.product_id)
    assert "package_amount" not in uf  # 567 was grams, not tortillas
    assert float(uf["g_per_unit"]) == 40
    g.add_stock(r.product_id, 10, None, None)
    assert g.stock_amounts()[parent] == 10


def test_child_in_parent_unit_keeps_package_amount():
    g = grocy()
    parent, piece = tortillas(g)
    off = {"status": 1, "product": {"product_name": "Mission Tortillas"}}
    r = resolve(g, FakeAI(guess=guess(generic_name="Tortillas", stock_unit="piece", package_amount=10,
                                      g_per_unit=45)), http(off=off), CODE)
    assert g.get_object("products", r.product_id)["qu_id_stock"] == piece
    assert float(g.userfields("products", r.product_id)["package_amount"]) == 10


def test_generics_sent_to_claude_with_units():
    seen = []

    class Spy(FakeAI):
        def guess_product(self, info, generics, locations, image=None):
            seen.append(generics)
            return super().guess_product(info, generics, locations, image)

    g = grocy()
    tortillas(g)
    resolve(g, Spy(guess=guess()), http(off=OFF), CODE)
    resolve_from_photo(g, Spy(guess=guess()), http(), "77", b"img")
    assert seen[0] == [{"name": "Tortillas", "stock_unit": "piece"}]
    assert {"name": "Tortillas", "stock_unit": "piece"} in seen[1]


def test_relink_to_generic_with_other_unit_aligns_units():
    g = grocy()
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)  # flour child in g, package 2268
    _, piece = tortillas(g, g_per_unit=40)
    relink(g, r.product_id, "Tortillas")
    child = g.get_object("products", r.product_id)
    assert all(child[k] == piece for k in ("qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price"))
    uf = g.userfields("products", r.product_id)
    assert not uf.get("package_amount") and float(uf["g_per_unit"]) == 40  # Grocy clears a userfield with ""


def test_relink_with_stock_in_another_unit_is_refused():
    import pytest
    g = grocy()
    r = resolve(g, FakeAI(guess=guess()), http(off=OFF), CODE)
    old_parent = g.get_object("products", r.product_id)["parent_product_id"]
    tortillas(g)
    g.add_stock(r.product_id, 500, None, None)
    with pytest.raises(ValueError, match="in stock"):
        relink(g, r.product_id, "Tortillas")
    assert g.get_object("products", r.product_id)["parent_product_id"] == old_parent
