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
    return ProductGuess(**{**d, **kw})


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
