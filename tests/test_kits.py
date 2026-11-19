"""Task 7: a recipe's ingredients added to inventory as that recipe's meal kit."""
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import ConversionGuess
from app.cfe import CfeTable, Ingredient, Op, line_text
from app.config import Settings
from app.house import HouseStore
from app.importer import PendingStore
from app.inventory import free_stock, kits, reserved
from app.mealplan import cook, plan_meal
from app.setup_grocy import bootstrap, ids
from app.shopping import rebuild_lists
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy
from tests.test_web import no_network, review_form, soup_draft

# a 2|4 EveryPlate card at its 2-serving column: 1 sauce packet, 1 sour cream packet, 2 chops; salt to taste
CARD = CfeTable(pre=[], ingredients=[
    Ingredient(qty=2, unit="tbsp", name="umami ginger sauce", packets=1),
    Ingredient(qty=None, unit="", name="sour cream", packets=1),
    Ingredient(qty=2, unit="", name="pork chops"),
    Ingredient(qty=None, unit="", name="salt"),
    Ingredient(qty=1, unit="tsp", name="paprika"),
], ops=[Op(id="o1", label="Sear 5 min, flip", inputs=["i0", "i1", "i2", "i3", "i4"])])


@pytest.fixture
def env(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    house = HouseStore(tmp_path / "house.db")
    ai = FakeAI(mode="free")  # adding a kit needs no Claude
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    store = PendingStore(tmp_path / "pending")
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(no_network)), settings, store, house=house)
    return g, house, ai, store, TestClient(app, cookies={"who": "kitchen"})


def product(g, name, unit="g", place="Fridge", days=5):
    i = ids(g)
    return g.create("products", {"name": name, "qu_id_stock": i.unit[unit], "location_id": i.location[place],
                                 "default_best_before_days": days})


def chops_recipe(g):
    """Herby pork chops, base 2 servings. Returns (rid, {name: product id})."""
    p = {"sauce": product(g, "Umami ginger sauce"), "sour": product(g, "Sour cream packet", "piece"),
         "chops": product(g, "Pork chops", "piece"), "salt": product(g, "Salt", place="Spice rack", days=0),
         "paprika": product(g, "Paprika", place="Spice rack")}
    g.set_userfields("products", p["sour"], {"measure": "packets", "measure_factor": 1})
    rid = g.create("recipes", {"name": "Herby pork chops", "base_servings": 2})
    amounts = {"sauce": 30, "sour": 0, "chops": 2, "salt": 0, "paprika": 2.5}
    for ing, key in zip(CARD.ingredients, p):
        g.create("recipes_pos", {"recipe_id": rid, "product_id": p[key], "amount": amounts[key],
                                 "qu_id": int(g.get_object("products", p[key])["qu_id_stock"]),
                                 "note": line_text(ing, 1)})
    g.set_userfields("recipes", rid, {"cfe_table": CARD.model_dump_json()})
    g.delete("products", p["paprika"])  # its position now has no product
    return rid, p


def add_kit(c, rid, servings=2):
    r = c.post(f"/recipes/{rid}/stock", data={"servings": servings}, follow_redirects=False)
    assert r.status_code == 303
    return r


def test_add_ingredients_confirm_lists_and_skips(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    page = c.get(f"/recipes/{rid}").text
    assert f'href="/recipes/{rid}/stock?servings=2"' in page and "Add ingredients to inventory" in page
    html = c.get(f"/recipes/{rid}/stock?servings=2").text
    assert re.search(r'2 tbsp</span> <span class="packet">1 packet</span> umami ginger sauce', html)
    assert re.search(r'<span class="packet">1 packet</span> sour cream', html)
    assert "2</span> pork chops" in html and "Fridge" in html
    skipped = html[html.index("Skipped"):]
    assert "salt" in skipped and "paprika" in skipped and "umami" not in skipped
    assert "Add to inventory" in html and f'href="/recipes/{rid}?servings=2"' in html and "Cancel" in html
    assert ' style="' not in html and "·" not in html
    assert g.stock_entries() == [] and kits(g, house) == {}  # the confirm page books nothing


def test_add_ingredients_books_scaled_amounts(env):
    """Review Focus 5: 3 servings of a 2|4 card show 1 1/2 packets and book the matching measured amount."""
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    html = c.get(f"/recipes/{rid}/stock?servings=3").text
    assert re.search(r'3 tbsp</span> <span class="packet">1 1/2 packets</span> umami ginger sauce', html)
    r = add_kit(c, rid, 3)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert urlsplit(r.headers["location"]).path == f"/recipes/{rid}" and q["msg"] == ["Added 3 ingredients."]
    assert len(q["undo"]) == 3
    soon = (date.today() + timedelta(days=5)).isoformat()
    fridge = ids(g).location["Fridge"]
    got = {e["product_id"]: (e["amount"], e["location_id"], e["best_before_date"]) for e in g.stock_entries()}
    assert got == {p["sauce"]: (45, fridge, soon), p["sour"]: (1.5, fridge, soon), p["chops"]: (3, fridge, soon)}
    assert kits(g, house) == {rid: {p["sauce"]: 45, p["sour"]: 1.5, p["chops"]: 3}}
    page = c.get(r.headers["location"]).text
    assert "Added 3 ingredients." in page and f'action="/recipes/{rid}/stock/undo"' in page
    add_kit(c, rid, 2)  # added again: it joins the same kit
    assert kits(g, house) == {rid: {p["sauce"]: 75, p["sour"]: 2.5, p["chops"]: 5}}


def test_add_ingredients_undo(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    tids = parse_qs(urlsplit(add_kit(c, rid, 2).headers["location"]).query)["undo"]
    # a kit item used by hand on Inventory shrinks the reservation
    c.post(f"/pantry/{p['chops']}/consume", data={"amount": 1, "kit": rid})
    assert kits(g, house)[rid][p["chops"]] == 1
    r = c.post(f"/recipes/{rid}/stock/undo", data={"tid": tids}, follow_redirects=False)
    msg = parse_qs(urlsplit(r.headers["location"]).query)["msg"][0]
    assert "Pork chops" in msg and "Umami" not in msg  # what couldn't be undone, by name
    assert {e["product_id"] for e in g.stock_entries()} == {p["chops"]}
    assert kits(g, house) == {rid: {p["chops"]: 1}}  # the rest stays consistent
    assert c.post(f"/recipes/{rid}/stock/undo", data={"tid": ["t999"]}).status_code == 200  # not this kit's: ignored
    c.post(f"/pantry/{p['chops']}/consume", data={"amount": 1, "kit": rid})
    assert kits(g, house) == {} and "Meal kits" not in c.get("/inventory").text  # all used: the kit is gone
    tids = parse_qs(urlsplit(add_kit(c, rid, 2).headers["location"]).query)["undo"]
    c.post(f"/recipes/{rid}/stock/undo", data={"tid": tids})
    assert g.stock_entries() == [] and kits(g, house) == {}  # undone in full: kit gone too


def test_add_ingredients_never_on_save(env):
    g, house, ai, store, c = env
    ai.mode_ = "full"
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.draft, ai.conversion = soup_draft(), ConversionGuess(factor=125, explanation="")
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    r = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg), follow_redirects=False)
    assert r.status_code == 303 and g.objects("recipes")
    assert g.stock_entries() == [] and house.kit_items() == []


def test_kit_items_listed_under_meal_kits_not_places(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    g.add_stock(p["chops"], 2, date.today() + timedelta(days=9), ids(g).location["Fridge"])  # our own
    add_kit(c, rid, 2)
    g.volatile_result["due_products"] = [{"product_id": p["sauce"], "amount": 30,
                                          "best_before_date": date.today().isoformat()}]
    html = c.get("/inventory").text
    kit = html[html.index("Meal kits"):html.index("<h2>Fridge</h2>")]
    assert "Herby pork chops: 3 items" in kit and "<details" in kit
    assert "Umami ginger sauce" in kit and "Fridge" in kit and re.search(r"Pork chops</b><span class=\"qty\">2 left", kit)
    fridge = html[html.index("<h2>Fridge</h2>"):]
    assert "Pork chops" in fridge and "2 left" in fridge and "4 left" not in fridge
    assert "Umami ginger sauce" not in fridge  # all of it is the kit's
    soon = html[html.index("Use soon"):html.index("Meal kits")]
    assert "Umami ginger sauce" in soon  # Use soon still includes kit items
    assert ' style="' not in html


def test_cook_uses_kit_entries_first(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    g.add_stock(p["chops"], 2, date.today(), ids(g).location["Fridge"])  # ours, sooner: Grocy's own order picks it
    add_kit(c, rid, 2)
    eid = plan_meal(g, date.today(), "Lunch", rid, 2, type("S", (), {"shopping_weekday": 6}), house=house)
    assert cook(g, eid, house) == []  # the sour cream packet too, 1 piece per packet
    assert kits(g, house) == {}  # the kit is used up
    assert sum(e["amount"] for e in g.stock[p["chops"]]) == 2  # our own chops are untouched


def test_other_recipes_and_shopping_ignore_kit_stock(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)
    add_kit(c, rid, 2)
    other = g.create("recipes", {"name": "Chop suey", "base_servings": 2})
    g.create("recipes_pos", {"recipe_id": other, "product_id": p["chops"], "amount": 2,
                             "qu_id": ids(g).unit["piece"], "note": "2 pork chops"})
    assert reserved(g, house) == {p["sauce"]: 30, p["sour"]: 1, p["chops"]: 2}
    assert free_stock(g, house)[p["chops"]] == 0
    s = type("S", (), {"shopping_weekday": 6})
    plan_meal(g, date.today(), "Lunch", rid, 2, s, house=house)
    assert p["chops"] not in rebuild_lists(g, date.today(), 6, house)  # a recipe with its own kit counts it
    eid = plan_meal(g, date.today() + timedelta(days=1), "Lunch", other, 2, s, house=house)
    assert rebuild_lists(g, date.today(), 6, house)[p["chops"]] == 2  # the other recipe can't count the box
    g.update("products", p["sauce"], {"min_stock_amount": 10})
    assert rebuild_lists(g, date.today(), 6, house)[p["sauce"]] == 10  # nor can Keep at least
    assert cook(g, eid, house) == ["Pork chops"]
    assert kits(g, house)[rid][p["chops"]] == 2  # the box is still there for its own recipe


def test_packets_shown_only_with_a_kit_and_switch_overrides(env):
    g, house, _, _, c = env
    rid, p = chops_recipe(g)

    def current(html):
        return re.findall(r'<a [^>]*aria-current="page"[^>]*>(Meal kit|Our pantry)</a>', html)

    html = c.get(f"/recipes/{rid}?servings=3").text
    assert 'class="packet"' not in html and current(html) == ["Our pantry"]
    assert f'href="/recipes/{rid}?servings=3&amp;kit=1"' in html  # the switch keeps the servings, no JS
    html = c.get(f"/recipes/{rid}?servings=3&kit=1").text
    assert '<span class="packet">1 1/2 packets</span>' in html and current(html) == ["Meal kit"]
    add_kit(c, rid, 2)
    html = c.get(f"/recipes/{rid}").text
    assert '<span class="packet">1 packet</span>' in html and current(html) == ["Meal kit"]
    html = c.get(f"/recipes/{rid}?kit=0").text
    assert 'class="packet"' not in html and current(html) == ["Our pantry"]
    assert ' style="' not in html
