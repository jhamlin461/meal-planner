"""The scan sheet's API: lookup changes nothing, confirm books the stock, undo reverses only that booking."""
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import ProductGuess
from app.config import Settings
from app.importer import PendingStore
from app.inventory import places
from app.setup_grocy import bootstrap, ids
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy
from tests.test_web import product, supply

OFF = json.loads((Path(__file__).parent / "fixtures" / "off_flour.json").read_text())


@pytest.fixture
def env(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    ai = FakeAI()
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=OFF) if "openfoodfacts" in r.url.host and "4242" in r.url.path
        else httpx.Response(404)))
    app = create_app(g, ai, http, Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "p"))
    return g, ai, TestClient(app, cookies={"who": "kitchen"})


def milk(g, package=946):
    i = ids(g)
    pid = product(g, "Brand Milk", "ml", parent_product_id=product(g, "Milk", "ml"),
                  location_id=i.location["Fridge"], default_best_before_days=7)
    if package:
        g.set_userfields("products", pid, {"package_amount": package})
    g.create("product_barcodes", {"product_id": pid, "barcode": "123"})
    return pid


def confirm(c, pid, **kw):
    return c.post("/api/scan/confirm", json={"product_id": pid, **kw})


def test_lookup_changes_no_stock(env):
    g, _, c = env
    pid = milk(g)
    g.add_stock(pid, 100, None, ids(g).location["Fridge"])
    r = c.post("/api/scan/lookup", json={"code": "123"}).json()
    assert r == {"status": "found", "product_id": pid, "product": "Brand Milk", "generic": "Milk", "unit": "ml",
                 "package": 946, "place": "Fridge", "places": list(places(g)), "kind": "food", "in_stock": 100,
                 "photo": None, "created": False, "need_min": False}
    assert g.stock_amounts()[pid] == 100 and len(g.stock[pid]) == 1
    assert c.post("/api/scan/lookup", json={"code": "999"}).json() == {"status": "unknown"}


def test_confirm_add_packs_into_chosen_place_keeps_default(env):
    g, _, c = env
    pid = milk(g)
    i = ids(g)
    r = confirm(c, pid, action="add", packs=2, place="Freezer").json()
    assert r["status"] == "ok" and r["transaction_id"]
    assert {k: r[k] for k in ("action", "product", "amount", "place")} == {
        "action": "add", "product": "Brand Milk", "amount": 1892, "place": "Freezer"}
    [e] = g.stock[pid]
    assert e["amount"] == 1892 and e["location_id"] == i.location["Freezer"]
    assert e["best_before_date"] == (date.today() + timedelta(days=7)).isoformat()
    assert g.get_object("products", pid)["location_id"] == i.location["Fridge"]  # the default place stays
    r = confirm(c, pid, action="add").json()  # one pack into the product's own place
    assert r["amount"] == 946 and r["place"] == "Fridge" and g.stock[pid][1]["location_id"] == i.location["Fridge"]


def test_confirm_add_unknown_package_uses_amount_and_remembers_it(env):
    g, _, c = env
    pid = milk(g, package=None)
    generic = g.get_object("products", pid)["parent_product_id"]
    assert c.post("/api/scan/lookup", json={"code": "123"}).json()["package"] is None
    assert confirm(c, pid, action="add").status_code == 400  # no size and no amount: nothing to book
    assert pid not in g.stock
    r = confirm(c, pid, action="add", amount=500).json()
    assert r["amount"] == 500 and g.stock_amounts()[pid] == 500
    assert g.userfields("products", pid)["package_amount"] == 500
    assert g.userfields("products", generic)["package_amount"] == 500
    assert c.post("/api/scan/lookup", json={"code": "123"}).json()["package"] == 500
    confirm(c, pid, action="add", amount=20)  # a typed amount never overwrites a known size
    assert g.userfields("products", pid)["package_amount"] == 500 and g.stock_amounts()[pid] == 520
    for bad in (0, -5, "inf"):
        assert confirm(c, pid, action="add", amount=bad).status_code == 422


def test_confirm_use_supply_one_per_pack(env):
    g, _, c = env
    bags = supply(g, stock=64, package_amount=32)
    r = confirm(c, bags, action="use", packs=3).json()
    assert r["status"] == "ok" and r["amount"] == 3 and r["place"] is None and r["transaction_id"]
    assert g.stock_amounts()[bags] == 61


def test_confirm_use_never_more_than_stock(env):
    g, _, c = env
    pid = milk(g, package=400)
    g.add_stock(pid, 500, None, None)
    assert confirm(c, pid, action="use", packs=2).json()["amount"] == 500
    assert g.stock[pid] == []
    r = confirm(c, pid, action="use").json()
    assert r == {"status": "ok", "action": "use", "product": "Brand Milk", "amount": 0, "place": None,
                 "transaction_id": None}
    for bad in (0, -1, 1.5, "inf"):  # packs are whole packages, at least one
        assert confirm(c, pid, action="use", packs=bad).status_code == 422


def test_old_instant_scan_endpoint_is_gone(env):
    g, _, c = env
    pid = milk(g)
    assert c.post("/api/scan", json={"code": "123", "action": "add"}).status_code in (404, 405)
    assert c.post(f"/api/products/{pid}/min", json={"amount": 2}).status_code in (404, 405)  # Keep at least rides on confirm
    assert pid not in g.stock


def test_confirm_rejects_unknown_place(env):
    g, _, c = env
    pid = milk(g)
    r = confirm(c, pid, action="add", place="Moon")
    assert r.status_code == 400 and r.json()["status"] == "error"
    assert pid not in g.stock
    assert confirm(c, 999, action="add").status_code == 404


def test_undo_reverses_add(env):
    g, _, c = env
    pid = milk(g)
    g.add_stock(pid, 10, None, None)
    tid = confirm(c, pid, action="add", packs=2).json()["transaction_id"]
    assert c.post("/api/scan/undo", json={"transaction_id": tid, "action": "add"}).json() == {"status": "ok"}
    assert g.stock_amounts()[pid] == 10
    assert c.post("/api/scan/undo", json={"transaction_id": tid, "action": "add"}).json()["status"] == "error"  # only once
    tid = confirm(c, pid, action="use").json()["transaction_id"]  # a use is put back
    assert g.stock_amounts().get(pid, 0) == 0
    assert c.post("/api/scan/undo", json={"transaction_id": tid, "action": "use"}).json() == {"status": "ok"}
    assert g.stock_amounts()[pid] == 10
    assert g.userfields("products", pid)["package_amount"] == 946  # only the booking is undone
    assert c.post("/api/scan/undo", json={"transaction_id": "../objects/products", "action": "add"}).status_code == 422
    assert c.post("/api/scan/undo", json={"transaction_id": tid, "action": "eat"}).status_code == 422


def test_undo_after_use_reports_already_used(env):
    g, _, c = env
    pid = milk(g)
    tid = confirm(c, pid, action="add").json()["transaction_id"]
    confirm(c, pid, action="use", amount=100)
    r = c.post("/api/scan/undo", json={"transaction_id": tid, "action": "add"}).json()
    assert r == {"status": "error", "message": "Can't undo: already used."}
    assert g.stock_amounts()[pid] == 846
    uid = confirm(c, pid, action="use", amount=100).json()["transaction_id"]
    g.bookings[uid] = ("undone", [])  # Grocy now refuses it
    r = c.post("/api/scan/undo", json={"transaction_id": uid, "action": "use"}).json()
    assert r == {"status": "error", "message": "Can't undo that one."}


def test_lookup_new_supply_flags_need_min(env):
    g, ai, c = env
    ai.guess = ProductGuess(generic_name="Trash bags", is_new_generic=True, stock_unit="piece", package_amount=40,
                            location="Garage", best_before_days=-1, g_per_unit=20, category="supply")
    r = c.post("/api/scan/lookup", json={"code": "4242"}).json()
    assert r["status"] == "found" and r["created"] and r["need_min"] and r["kind"] == "supply"
    assert r["place"] == "Garage" and r["package"] == 40 and r["in_stock"] == 0
    pid = r["product_id"]
    assert pid not in g.stock
    r = confirm(c, pid, action="add", min=2).json()
    assert r["amount"] == 40 and g.stock_amounts()[pid] == 40
    generic = g.get_object("products", g.get_object("products", pid)["parent_product_id"])
    assert generic["min_stock_amount"] == 2 and generic["cumulate_min_stock_amount_of_sub_products"] == 1
    assert g.stock[pid][0]["best_before_date"] is None  # supplies never expire
    assert c.post("/api/scan/lookup", json={"code": "4242"}).json()["need_min"] is False


def test_manual_and_photo_return_found_without_adding(env):
    g, ai, c = env
    ai.guess = ProductGuess(generic_name="Beans", is_new_generic=True, stock_unit="g", package_amount=400,
                            location="Pantry", best_before_days=700, g_per_unit=1)
    r = c.post("/api/scan/photo", data={"code": "555"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "found" and r["created"] and r["generic"] == "Beans" and r["package"] == 400
    assert r["place"] == "Pantry" and r["in_stock"] == 0 and r["product_id"] not in g.stock
    form = {"code": "111", "name": "Jar of jam", "generic": "Jam", "stock_unit": "g", "package_amount": "300",
            "location": "Counter", "category": "food"}
    r = c.post("/api/scan/manual", data=form).json()
    assert r["status"] == "found" and r["created"] and r["package"] == 300 and r["place"] == "Counter"
    assert r["product_id"] not in g.stock
