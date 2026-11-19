import re
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.importer import PendingStore, process
from app.inventory import add_place, default_place, places, remove_place, rename_place, rows
from app.setup_grocy import LOCATIONS, bootstrap, ids, supplies_group
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


def test_remove_place_needs_a_real_target_and_keeps_one():
    g = FakeGrocy()
    bootstrap(g)
    bid = add_place(g, "Basement")
    for bad in (bid, 9999):
        with pytest.raises(ValueError, match="Pick another place to move products to."):
            remove_place(g, bid, bad)
    for name, pid in list(places(g).items())[:-1]:
        remove_place(g, pid, bid)
    with pytest.raises(ValueError, match="Keep at least one place."):
        remove_place(g, bid, bid)
    assert list(places(g)) == ["Basement"]


# ---- the Inventory page -------------------------------------------------------------------------------------------
SETTINGS = Settings("", "", "", "KEY", None, 6, Path("."), 3)


def food(g, name, place, amount=0, unit="g", bb=None):
    i = ids(g)
    pid = g.create("products", {"name": name, "qu_id_stock": i.unit[unit], "location_id": i.location[place]})
    if amount:
        g.add_stock(pid, amount, bb, i.location[place])
    return pid


def supply(g, name, place, amount=0, min_stock=0, **kw):
    i = ids(g)
    pid = g.create("products", {"name": name, "qu_id_stock": i.unit["piece"], "location_id": i.location[place],
                                "product_group_id": supplies_group(g), "min_stock_amount": min_stock, **kw})
    if amount:
        g.add_stock(pid, amount, None, i.location[place])
    return pid


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_inventory_groups_food_and_supplies_by_place(env):
    g, c = env
    yogurt = food(g, "Greek yogurt", "Fridge", 500, bb=date.today() + timedelta(days=20))
    towels = supply(g, "Paper towels", "Closet", 3, min_stock=2)
    soap = supply(g, "Dish soap", "Under sink", min_stock=1)  # none anywhere: still shows, at its default place
    food(g, "Rice", "Pantry")  # no stock: food doesn't show
    inv = rows(g, SETTINGS, date.today())
    assert list(inv.groups) == ["Fridge", "Under sink", "Closet"]  # places() order, empty places left out
    [y] = inv.groups["Fridge"]
    assert (y["pid"], y["name"], y["amount"], y["unit"], y["kind"]) == (yogurt, "Greek yogurt", 500, "g", "food")
    [t] = inv.groups["Closet"]
    assert (t["pid"], t["amount"], t["total"], t["min"], t["low"], t["kind"]) == (towels, 3, 3, 2, False, "supply")
    [s] = inv.groups["Under sink"]
    assert (s["pid"], s["amount"], s["total"], s["low"]) == (soap, 0, 0, True)
    html = c.get("/inventory").text
    assert "Rice" not in html and html.index("<h2>Fridge</h2>") < html.index("<h2>Under sink</h2>") \
        < html.index("<h2>Closet</h2>")
    assert f'action="/pantry/{yogurt}/consume"' in html and f'action="/pantry/{yogurt}/amount"' in html
    assert f'action="/home/supplies/{towels}/used"' in html and f'action="/home/supplies/{towels}/min"' in html
    assert f'action="/pantry/{towels}/amount"' not in html  # no Set total for supplies
    assert "Running low" in text(html) and "Used one" in html and "<summary>More</summary>" in html
    assert 'href="/inventory/places"' in html and "Manage places" in html


def test_supply_row_under_place_with_most_stock(env):
    """Pre-flight ruling (replaces "under the place with most stock"): a supply has one row per place holding it or
    its brands, with that place's share, and Running low uses the generic's total across places."""
    g, c = env
    bags = supply(g, "Trash bags", "Garage", min_stock=6, cumulate_min_stock_amount_of_sub_products=1)
    supply(g, "Glad bags", "Garage", 3, parent_product_id=bags)
    supply(g, "Hefty bags", "Closet", 2, parent_product_id=bags)
    inv = rows(g, SETTINGS, date.today())
    [garage], [closet] = inv.groups["Garage"], inv.groups["Closet"]
    assert (garage["pid"], garage["amount"], garage["total"], garage["low"]) == (bags, 3, 5, True)
    assert (closet["pid"], closet["amount"], closet["total"], closet["low"]) == (bags, 2, 5, True)
    html = c.get("/inventory").text
    assert "Glad bags" not in html and "Hefty bags" not in html
    assert html.count('<b class="grow breakable">Trash bags</b>') == 2 and "5 in all" in text(html)


def test_inventory_place_chip_filters(env):
    g, c = env
    food(g, "Greek yogurt", "Fridge", 500)
    supply(g, "Paper towels", "Closet", 3)
    p = places(g)
    html = c.get("/inventory").text
    chips = html[html.index('class="place-chips"'):]
    chips = chips[:chips.index("</nav>")]
    assert re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', chips) == [
        ("/inventory", "All"), (f"/inventory?place={p['Fridge']}", "Fridge"),
        (f"/inventory?place={p['Closet']}", "Closet")]
    assert '<a href="/inventory" aria-current="page">All</a>' in chips and chips.count("aria-current") == 1
    html = c.get(f"/inventory?place={p['Closet']}").text
    assert "<h2>Closet</h2>" in html and "<h2>Fridge</h2>" not in html and "Greek yogurt" not in html
    assert f'<a href="/inventory?place={p["Closet"]}" aria-current="page">Closet</a>' in html
    assert f'name="place" value="{p["Closet"]}"' in html  # its forms come back to the same filter
    html = c.get(f"/inventory?place={p['Freezer']}").text  # a place with nothing in it: everything
    assert "<h2>Closet</h2>" in html and "<h2>Fridge</h2>" in html


def test_old_pantry_and_supplies_urls_redirect(env):
    _, c = env
    for url in ("/pantry", "/home/supplies"):
        r = c.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inventory", url
    for url in ("/pantry", "/home/supplies"):
        r = c.get(f"{url}?msg=Used+one+Sponges.", follow_redirects=False)
        assert r.headers["location"] == "/inventory?msg=Used+one+Sponges.", url


def test_inventory_forms_return_to_inventory(env):
    g, c = env
    p = places(g)
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    bags = supply(g, "Trash bags", "Garage", min_stock=2)
    hefty = supply(g, "Hefty bags", "Closet", 4, parent_product_id=bags)
    fridge = f"/inventory?place={p['Fridge']}"

    def post(url, **data):
        r = c.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303, url
        return r.headers["location"]

    assert post(f"/pantry/{yogurt}/consume", amount=100) == "/inventory"
    assert post(f"/pantry/{yogurt}/consume", amount=100, place=p["Fridge"]) == fridge
    assert post(f"/pantry/{yogurt}/amount", amount=250, place=p["Fridge"]) == fridge
    assert g.stock_amounts()[yogurt] == 250
    assert post(f"/pantry/{yogurt}/move", from_location=p["Fridge"], to_location=p["Freezer"], amount=250) == "/inventory"
    assert post(f"/home/supplies/{bags}/used") == "/inventory?msg=Used+one+Trash+bags."
    assert post(f"/home/supplies/{bags}/used", place=p["Closet"]) == \
        f"/inventory?place={p['Closet']}&msg=Used+one+Trash+bags."
    assert post(f"/home/supplies/{bags}/min", amount=3) == "/inventory"
    assert g.get_object("products", bags)["min_stock_amount"] == 3
    assert post(f"/home/supplies/{bags}/move", from_location=p["Closet"], to_location=p["Garage"]) == "/inventory"
    assert {e["location_id"] for e in g.stock[hefty]} == {p["Garage"]} and g.stock_amounts()[bags] == 2
    assert "Used one Trash bags." in c.get("/inventory?msg=Used+one+Trash+bags.").text


def test_nav_tabs(env):
    _, c = env
    html = c.get("/inventory").text
    nav = html[html.index('class="rail"'):html.index("</nav>")]
    assert re.findall(r'<li><a href="([^"]+)"[^>]*>.*?<span>([^<]+)</span>', nav) == [
        ("/", "Today"), ("/plan", "Meals"), ("/inventory", "Inventory"), ("/home", "Chores")]
    assert 'href="/inventory" aria-current="page"' in nav
    plan = c.get("/plan").text
    meals = plan[plan.index('aria-label="Meals pages"'):]
    assert re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', meals[:meals.index("</nav>")]) == [
        ("/plan", "Plan"), ("/recipes", "Recipes"), ("/shopping", "Shopping")]
    for url in ("/inventory", "/home", "/plan", "/scan", "/scan?from=chores", "/inventory/places"):
        html = c.get(url).text
        assert 'href="/pantry"' not in html and 'href="/home/supplies"' not in html and ">Supplies<" not in html, url
    assert 'class="segmented pages"' not in c.get("/home").text  # Chores is one page
    for url in ("/scan", "/scan?from=chores", "/inventory/places"):
        assert 'href="/inventory" aria-current="page"' in c.get(url).text, url


def test_inventory_no_inline_styles_and_scan_action(env):
    g, c = env
    html = c.get("/inventory").text
    assert "style=" not in html and "·" not in html and "→" not in html
    assert re.search(r'<div class="title-row"><h1>Inventory</h1>\s*<a class="button title-action" href="/scan">', html)
    assert "Nothing here yet. Scan something to add it." in text(html) and '<a href="/scan">Scan something</a>' in html
    food(g, "Greek yogurt", "Fridge", 500)
    html = c.get("/inventory").text
    assert "style=" not in html and "Nothing here yet" not in html
    for url in ("/plan", "/home", "/scan", "/inventory/places"):
        assert "title-action" not in c.get(url).text, url


def uses(g, pid, recipe=None, chore=None, consume=1):
    if recipe:
        g.create("recipes_pos", {"recipe_id": g.create("recipes", {"name": recipe}), "product_id": pid})
    if chore:
        g.create("chores", {"name": chore, "product_id": pid, "consume_product_on_execution": consume})


def test_clear_stock_keeps_product(env):
    g, c = env
    bags = supply(g, "Trash bags", "Garage")
    hefty = supply(g, "Hefty bags", "Closet", 4, parent_product_id=bags)
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    r = c.post(f"/inventory/{yogurt}/clear", follow_redirects=False)
    assert r.headers["location"] == "/inventory?msg=Cleared+Greek+yogurt."
    assert g.stock_amounts().get(yogurt, 0) == 0 and g.get_object("products", yogurt)
    c.post(f"/inventory/{bags}/clear")
    assert g.stock_amounts().get(bags, 0) == 0 and g.get_object("products", hefty)


def test_remove_product_deletes_product_barcodes_and_list_rows(env):
    g, c = env
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    other = food(g, "Milk", "Fridge", 1)
    for pid in (yogurt, other):
        g.create("product_barcodes", {"product_id": pid, "barcode": f"c{pid}"})
        g.create("shopping_list", {"product_id": pid, "amount": 1})
    r = c.post(f"/inventory/{yogurt}/remove", follow_redirects=False)
    assert r.headers["location"] == "/inventory?msg=Removed+Greek+yogurt."
    assert g.get_object("products", yogurt) is None and g.stock_amounts().get(yogurt, 0) == 0
    assert [b["product_id"] for b in g.objects("product_barcodes")] == [other]
    assert [b["product_id"] for b in g.objects("shopping_list")] == [other]


def test_remove_refused_when_recipe_or_chore_uses_it(env):
    g, c = env
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    uses(g, yogurt, recipe="Fudgy brownies", chore="Change the litter")
    uses(g, yogurt, chore="Water plants", consume=0)  # does not use it up
    g.create("product_barcodes", {"product_id": yogurt, "barcode": "x"})
    r = c.post(f"/inventory/{yogurt}/remove", follow_redirects=True)
    assert r.status_code == 200
    assert "Fudgy brownies and Change the litter use this. Change them first, or use Clear stock." in r.text
    assert 'role="alert"' in r.text and "Water plants" not in r.text
    assert g.get_object("products", yogurt) and g.stock_amounts()[yogurt] == 500 and g.objects("product_barcodes")


def test_remove_brand_ignores_generic_users(env):
    g, c = env
    bags = supply(g, "Trash bags", "Garage")
    hefty = supply(g, "Hefty bags", "Closet", 4, parent_product_id=bags)
    uses(g, bags, recipe="Bag lunch")
    c.post(f"/inventory/{hefty}/remove")
    assert g.get_object("products", hefty) is None and g.get_object("products", bags)


def test_remove_generic_with_brands_refused(env):
    g, c = env
    bags = supply(g, "Trash bags", "Garage")
    hefty = supply(g, "Hefty bags", "Closet", 4, parent_product_id=bags)
    uses(g, hefty, chore="Take out trash")
    assert "Take out trash uses this." in c.post(f"/inventory/{bags}/remove", follow_redirects=True).text
    assert g.get_object("products", hefty) and g.get_object("products", bags) and g.stock_amounts()[hefty] == 4
    g.delete("chores", g.objects("chores")[0]["id"])
    c.post(f"/inventory/{bags}/remove")
    assert not g.objects("products", name="Hefty bags") and not g.objects("products", name="Trash bags")
    assert g.stock_amounts().get(hefty, 0) == 0


def test_inventory_more_has_clear_and_remove(env):
    g, c = env
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    bags = supply(g, "Trash bags", "Garage")
    html = c.get("/inventory").text
    for pid in (yogurt, bags):
        assert f'action="/inventory/{pid}/clear"' in html and f'href="/inventory/{pid}/remove"' in html
    assert "onsubmit" not in html
    assert "You have none left. It stays known, so scanning it again fills in the details, and it can still go on the shopping list." in html
    assert 'class="btn danger"' in html and "style=" not in html


def test_remove_confirm_page(env):
    g, c = env
    p = places(g)
    yogurt = food(g, "Greek yogurt", "Fridge", 500)
    r = c.get(f"/inventory/{yogurt}/remove?place={p['Fridge']}")
    assert r.status_code == 200 and "style=" not in r.text and "onsubmit" not in r.text
    assert "Remove Greek yogurt? Its stock, barcode and details are deleted. This can't be undone." in r.text
    assert f'action="/inventory/{yogurt}/remove"' in r.text and f'value="{p["Fridge"]}"' in r.text
    assert 'href="/inventory' in r.text and g.get_object("products", yogurt)  # a GET deletes nothing
    uses(g, yogurt, recipe="Fudgy brownies")
    r = c.get(f"/inventory/{yogurt}/remove")
    assert 'role="alert"' in r.text and "Fudgy brownies uses this." in r.text and f'action="/inventory/{yogurt}/remove"' not in r.text
    assert c.get("/inventory/9999/remove").status_code == 404


def test_remove_grocy_refusal_keeps_barcodes_and_list_rows(env):
    from app.grocy import GrocyError
    g, c = env
    bags = supply(g, "Trash bags", "Garage")
    a = supply(g, "Hefty bags", "Closet", parent_product_id=bags)
    b = supply(g, "Glad bags", "Closet", 2, parent_product_id=bags)
    for pid in (a, b, bags):
        g.create("product_barcodes", {"product_id": pid, "barcode": f"c{pid}"})
        g.create("shopping_list", {"product_id": pid, "amount": 1})
    real = g.delete

    def delete(entity, id):
        if entity == "products" and id == b:
            raise GrocyError("nope", 400, "still in a recipe")
        real(entity, id)
    g.delete = delete
    r = c.post(f"/inventory/{bags}/remove", follow_redirects=True)
    assert r.status_code == 200 and 'role="alert"' in r.text
    assert "Grocy wouldn&#39;t remove Glad bags: still in a recipe. Removed Hefty bags first." in r.text
    assert {x["product_id"] for x in g.objects("product_barcodes")} == {b, bags}
    assert {x["product_id"] for x in g.objects("shopping_list")} == {b, bags}
    assert g.get_object("products", b) and g.get_object("products", bags) and not g.get_object("products", a)
