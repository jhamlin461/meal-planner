import json

import httpx
import pytest

from app.config import load_settings
from app.grocy import Grocy, GrocyError


def test_create_returns_id_and_sends_key():
    seen = {}
    def handler(req):
        seen["key"] = req.headers["GROCY-API-KEY"]; seen["url"] = str(req.url)
        return httpx.Response(200, json={"created_object_id": "7"})
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.create("products", {"name": "Flour"}) == 7
    assert seen == {"key": "k", "url": "http://g/api/objects/products"}


def test_objects_builds_query():
    def handler(req):
        assert req.url.params.get_list("query[]") == ["name=Flour"]
        return httpx.Response(200, json=[{"id": 1}])
    assert Grocy("http://g/api", "k", transport=httpx.MockTransport(handler)).objects("products", name="Flour") == [{"id": 1}]


def test_product_by_barcode_unknown_is_none():
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error_message": "x"})))
    assert g.product_by_barcode("123") is None


def test_stock_amounts_roll_children_into_parent():
    def handler(req):
        if req.url.path.endswith("/stock"):
            return httpx.Response(200, json=[{"product_id": 2, "amount": "500"}, {"product_id": 1, "amount": "100"}])
        return httpx.Response(200, json=[{"id": 1, "parent_product_id": None}, {"id": 2, "parent_product_id": 1}])
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.stock_amounts() == {1: 600.0, 2: 500.0}


def test_error_status_raises():
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(GrocyError):
        g.objects("products")


def test_consume_body_and_set_amount_and_add_stock():
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.read().decode()))
        return httpx.Response(200, json={})
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    g.consume(3, 2.5)
    g.set_amount(3, 9)
    from datetime import date
    g.add_stock(3, 1, date(2026, 12, 1), 4)
    assert '"spoiled":false' in calls[0][2].replace(" ", "") and calls[0][1] == "/api/stock/products/3/consume"
    assert calls[1][1] == "/api/stock/products/3/inventory"
    assert "2026-12-01" in calls[2][2] and calls[2][1] == "/api/stock/products/3/add"


def test_load_settings_defaults(monkeypatch):
    for k in ("GROCY_URL", "GROCY_API_KEY", "ANTHROPIC_API_KEY", "FDC_API_KEY", "WALMART_STORE_ID",
              "SHOPPING_WEEKDAY", "DATA_DIR", "EXPIRY_WARN_DAYS"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.fdc_api_key, s.shopping_weekday, s.expiry_warn_days, s.walmart_store_id) == ("DEMO_KEY", 6, 3, None)
    assert str(s.data_dir).replace("\\", "/").endswith("/data")


def test_fake_grocy_roundtrip():
    from tests.fakes import FakeGrocy
    f = FakeGrocy()
    parent = f.create("products", {"name": "Flour"})
    child = f.create("products", {"name": "AP Flour", "parent_product_id": parent})
    f.add_stock(child, 500, None, None)
    f.add_stock(parent, 100, None, None)
    assert f.stock_amounts() == {parent: 600.0, child: 500.0}
    f.consume(child, 200)
    assert f.stock_amounts()[child] == 300.0
    assert f.objects("products", name="Flour")[0]["id"] == parent


def test_transfer_posts_locations():
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, req.read()
        return httpx.Response(200, json=[])
    Grocy("http://g/api", "k", transport=httpx.MockTransport(handler)).transfer(5, 2.5, 1, 3)
    assert seen["path"] == "/api/stock/products/5/transfer"
    assert json.loads(seen["body"]) == {"amount": 2.5, "location_id_from": 1, "location_id_to": 3}


def test_create_user_posts_then_finds_id():
    seen = []

    def h(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json=[{"id": 1, "username": "admin"}, {"id": 5, "username": "josh"}])

    g = Grocy("http://g", "k", transport=httpx.MockTransport(h))
    assert g.create_user("josh", "Josh") == 5
    body = seen[0][2]
    assert seen[0][:2] == ("POST", "/users") and body["username"] == "josh" and body["first_name"] == "Josh"
    assert len(body["password"]) >= 20


def test_create_user_not_found_raises():
    h = lambda req: httpx.Response(204) if req.method == "POST" else httpx.Response(200, json=[])  # noqa: E731
    with pytest.raises(GrocyError, match="user not created"):
        Grocy("http://g", "k", transport=httpx.MockTransport(h)).create_user("josh", "Josh")


def test_execute_chore_posts_done_by():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path; seen["body"] = json.loads(req.read())
        return httpx.Response(204)
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    g.execute_chore(3, 2, skipped=True)
    assert seen["path"] == "/api/chores/3/execute"
    assert seen["body"]["done_by"] == 2 and seen["body"]["skipped"] is True and seen["body"]["tracked_time"]
    g.execute_chore(3, None)
    assert "done_by" not in seen["body"]


def test_open_tasks_and_complete():
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.read().decode()))
        return httpx.Response(200, json=[{"id": 4}]) if req.method == "GET" else httpx.Response(204)
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.open_tasks() == [{"id": 4}]
    g.complete_task(4)
    assert calls[1][:2] == ("POST", "/api/tasks/4/complete") and "done_time" in calls[1][2]


def test_battery_charge():
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.read().decode()))
        return httpx.Response(200, json=[{"battery_id": 1}]) if req.method == "GET" else httpx.Response(204)
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.batteries_status() == [{"battery_id": 1}]
    g.charge_battery(1)
    assert calls[1][:2] == ("POST", "/api/batteries/1/charge") and "tracked_time" in calls[1][2]


def test_recipe_picture_put_and_get():
    import base64
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params), req.headers.get("content-type"), req.content))
        if "missing" in base64.b64decode(req.url.path.rsplit("/", 1)[1]).decode():
            return httpx.Response(404)
        if req.method == "GET":
            return httpx.Response(200, content=b"JPEG", headers={"content-type": "image/jpeg"})
        return httpx.Response(204)

    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    g.put_recipe_picture("recipe-1-ab.jpg", b"data")
    b64 = base64.b64encode(b"recipe-1-ab.jpg").decode()
    assert seen[0] == ("PUT", f"/api/files/recipepictures/{b64}", {}, "application/octet-stream", b"data")
    assert g.recipe_picture("recipe-1-ab.jpg", 400) == (b"JPEG", "image/jpeg")
    assert seen[1][2] == {"force_serve_as": "picture", "best_fit_width": "400"}
    assert g.recipe_picture("missing.jpg", 400) is None
    g.delete_recipe_picture("recipe-1-ab.jpg")
    assert seen[-1][:2] == ("DELETE", f"/api/files/recipepictures/{b64}")
