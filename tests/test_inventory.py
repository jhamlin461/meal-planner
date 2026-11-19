import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.importer import PendingStore, process
from app.inventory import add_place, default_place, places, remove_place, rename_place
from app.setup_grocy import LOCATIONS, bootstrap, ids
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy
from tests.test_budget import import_app, jsonld_http


@pytest.fixture
def env(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    app = create_app(g, FakeAI(mode="free"), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"))
    return g, TestClient(app, cookies={"who": "kitchen"})


def test_places_order_starting_first():
    g = FakeGrocy()
    g.create("locations", {"name": "Zebra room"})
    g.create("locations", {"name": "Attic"})
    bootstrap(g)  # not empty: adds nothing
    g.create("locations", {"name": "Pantry"})
    assert list(places(g)) == ["Pantry", "Attic", "Zebra room"]
    assert default_place(g) == "Pantry"
    h = FakeGrocy()
    bootstrap(h)
    assert list(places(h)) == LOCATIONS
    h.create("locations", {"name": "Basement"})
    assert list(places(h)) == [*LOCATIONS, "Basement"]


def test_bootstrap_seeds_places_only_on_empty_grocy():
    g = FakeGrocy()
    g.create("locations", {"name": "Kitchen shelf"})
    bootstrap(g)
    assert list(places(g)) == ["Kitchen shelf"] and default_place(g) == "Kitchen shelf"
    h = FakeGrocy()
    bootstrap(h)
    assert set(places(h)) == set(LOCATIONS)


def test_add_rename_remove_place():
    g = FakeGrocy()
    bootstrap(g)
    with pytest.raises(ValueError, match="There's already a place called pantry."):
        add_place(g, "pantry")
    with pytest.raises(ValueError, match="Give the place a name."):
        add_place(g, "  ")
    bid = add_place(g, "Basement")
    rename_place(g, bid, "Cellar")
    assert "Cellar" in places(g) and "Basement" not in places(g)
    rename_place(g, bid, "cellar")  # its own name in another case is fine
    with pytest.raises(ValueError, match="There's already a place called Fridge."):
        rename_place(g, bid, "Fridge")
    i = ids(g)
    stocked = g.create("products", {"name": "Jam", "location_id": bid})
    g.add_stock(stocked, 1, None, bid)
    with pytest.raises(ValueError, match="Move or use up what's in cellar first."):
        remove_place(g, bid, i.location["Pantry"])
    g.consume(stocked, 1)
    other = g.create("products", {"name": "Rice", "location_id": bid})
    remove_place(g, bid, i.location["Pantry"])
    assert "cellar" not in places(g)
    assert g.get_object("products", other)["location_id"] == i.location["Pantry"]
    assert g.get_object("products", stocked)["location_id"] == i.location["Pantry"]


def test_manage_places_page(env):
    g, c = env
    html = c.get("/inventory/places").text
    assert "Manage places" in html and "style=" not in html and 'action="/inventory/places"' in html
    r = c.post("/inventory/places", data={"name": "Basement"}, follow_redirects=False)
    assert r.status_code == 303 and "Basement" in places(g)
    bid = places(g)["Basement"]
    c.post(f"/inventory/places/{bid}/rename", data={"name": "Cellar"})
    assert "Cellar" in places(g)
    r = c.post("/inventory/places", data={"name": "Cellar"})
    assert r.status_code == 400 and 'role="alert"' in r.text and "There&#39;s already a place called Cellar." in r.text
    g.add_stock(g.create("products", {"name": "Jam", "location_id": bid}), 1, None, bid)
    r = c.post(f"/inventory/places/{bid}/remove", data={"move_to": places(g)["Pantry"]})
    assert r.status_code == 400 and "Move or use up what&#39;s in Cellar first." in r.text
    g.stock.clear()
    assert c.post(f"/inventory/places/{bid}/remove", data={"move_to": places(g)["Pantry"]}).status_code == 200
    assert "Cellar" not in places(g)


def test_scan_manual_accepts_a_new_place(env):
    g, c = env
    form = {"code": "111", "name": "Jar", "generic": "Jam", "stock_unit": "g", "package_amount": "300",
            "location": "Basement", "category": "food"}
    assert c.post("/api/scan/manual", data=form).status_code == 400
    add_place(g, "Basement")
    r = c.post("/api/scan/manual", data=form)
    assert r.json()["status"] == "ok"
    assert g.get_object("products", r.json()["product_id"])["location_id"] == places(g)["Basement"]
    assert "Basement" in c.get("/scan").text


def test_review_manual_product_accepts_new_place(tmp_path):
    ai = FakeAI(mode="free")
    c, store = import_app(tmp_path, ai)
    g = c.grocy
    add_place(g, "Basement")
    p = store.new("https://example.com/soup")
    process(store, ai, jsonld_http(), p.id, "https://example.com/soup", None)
    assert "Basement" in c.get(f"/import/{p.id}").text
    form = {"new_name_0": "Crushed tomatoes", "generic_0": "Crushed tomatoes", "stock_unit_0": "g",
            "package_0": "794", "location_0": "Basement", "category_0": "food", "action": "create:0"}
    c.post(f"/import/{p.id}", data=form)
    tom = next(x for x in g.objects("products") if x["name"] == "Crushed tomatoes")
    assert tom["location_id"] == places(g)["Basement"]
