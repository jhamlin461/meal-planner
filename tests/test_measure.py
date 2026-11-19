"""Household measures: every amount a person sees or types is in the item's measure; Grocy keeps stock units."""
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import measure
from app.ai import MeasureGuess, ProductGuess
from app.config import Settings
from app.importer import PendingStore
from app.measure import Measure
from app.setup_grocy import bootstrap, ids
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy
from tests.test_web import product, supply


@pytest.fixture
def env(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    ai = FakeAI()
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "p"))
    return g, ai, TestClient(app, cookies={"who": "kitchen"})


def measured(g, pid, word, factor, source="estimate"):
    g.set_userfields("products", pid, {"measure": word, "measure_factor": factor, "measure_source": source})


def test_kitchen_math():
    cup, tbsp = Measure("cups", measure.KITCHEN["cup"][1]), Measure("tbsp", measure.KITCHEN["tbsp"][1])
    assert measure.show(measure.to_stock(2, tbsp), cup) == "1/8 cup"
    oz, lb = Measure("oz", measure.KITCHEN["oz"][1]), Measure("lb", measure.KITCHEN["lb"][1])
    assert measure.show(measure.to_stock(16, oz), lb) == "1 lb"
    g = FakeGrocy()
    bootstrap(g)
    flour = product(g, "Flour")  # stocked in g, 125 g a cup
    measured(g, flour, "cups", 125)
    p = g.get_object("products", flour)
    assert measure.change(g, p, "tbsp").factor == pytest.approx(125 * 14.79 / 236.6)  # within volume: fixed math
    assert measure.change(g, p, "oz").factor == pytest.approx(28.35)  # weight of a g product: fixed math
    assert measure.change(g, p, "lb").factor == pytest.approx(453.6)
    assert measure.change(g, p, "cups").factor == pytest.approx(125)  # its own cup weight was kept


def test_show_rounds_to_kitchen_fractions():
    cups = Measure("cups", 120)
    assert measure.show(300, cups) == "2 1/2 cups"
    assert measure.show(360, cups) == "3 cups"
    assert measure.show(40, cups) == "1/3 cup"
    assert measure.show(84, cups) == "2/3 cup"  # 0.7 cup: the nearest kitchen fraction
    assert measure.show(14 * 120 + 7, cups) == "14 cups"
    assert measure.show(2, cups) == "less than 1/8 cup"
    rolls = Measure("rolls", 1)
    assert (measure.show(1, rolls), measure.show(3, rolls)) == ("1 roll", "3 rolls")
    assert measure.show(500, Measure("g", 1)) == "500 g" and measure.show(12, Measure("piece", 1)) == "12"
    assert measure.to_stock(1.5, cups) == 180


def test_measure_estimated_once_and_cached():
    g = FakeGrocy()
    bootstrap(g)
    ai = FakeAI(measure=MeasureGuess(measure="cups", factor=125))
    flour = g.get_object("products", product(g, "Flour"))
    assert measure.of(g, ai, flour) == Measure("cups", 125)
    assert measure.of(g, ai, flour) == Measure("cups", 125) and len(ai.measures) == 1
    assert ai.measures == [("Flour", "g")]
    assert {k: g.userfields("products", flour["id"])[k] for k in ("measure", "measure_factor", "measure_source")} == \
        {"measure": "cups", "measure_factor": 125, "measure_source": "estimate"}
    milk = g.get_object("products", product(g, "Milk", "ml"))
    ai.measure = MeasureGuess(measure="Cup", factor=240)  # a volume of an ml product: kitchen math, not Claude's
    assert measure.of(g, ai, milk) == Measure("cups", 236.6)
    eggs = g.get_object("products", product(g, "Eggs", "piece"))
    ai.measure = MeasureGuess(measure="eggs", factor=1)
    assert measure.of(g, ai, eggs) == Measure("eggs", 1)
    assert measure.choices("piece", Measure("eggs", 1)) == ["cups", "tbsp", "tsp", "fl oz", "oz", "lb", "eggs"]
    assert measure.choices("g", Measure("cups", 125)) == ["cups", "tbsp", "tsp", "fl oz", "oz", "lb"]
    ai.error = RuntimeError("never called")  # stored: Claude isn't asked again
    assert measure.of(g, ai, eggs) == Measure("eggs", 1)


def test_new_product_gets_measure_at_creation(env):
    g, ai, c = env
    ai.guess = ProductGuess(generic_name="Paper towels", is_new_generic=True, stock_unit="piece", package_amount=6,
                            location="Closet", best_before_days=-1, g_per_unit=100, category="supply",
                            measure="rolls", measure_factor=1)
    pid = measure_scan(c, "777")
    uf = g.userfields("products", pid)
    assert (uf["measure"], uf["measure_factor"], uf["measure_source"]) == ("rolls", 1, "estimate")
    assert uf["package_source"] == "estimate"  # Claude read the size off the photo
    assert not ai.measures  # no second call


def measure_scan(c, code):
    """A new product from a barcode the fake Open Food Facts doesn't know: made from a photo."""
    r = c.post("/api/scan/photo", data={"code": code}, files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")})
    return r.json()["product_id"]


def test_change_measure_owner_wins(env):
    g, ai, c = env
    flour = product(g, "Flour")
    g.add_stock(flour, 500, None, ids(g).location["Pantry"])
    measured(g, flour, "cups", 125)
    html = c.get("/inventory").text
    assert "4 cups left" in html and "Measure in" in html
    assert '<option value="oz">oz</option>' in html and '<option value="cups" selected>cups</option>' in html
    r = c.post(f"/inventory/{flour}/measure", data={"measure": "oz"}, follow_redirects=False)
    assert r.status_code == 303
    uf = g.userfields("products", flour)
    assert (uf["measure"], uf["measure_source"]) == ("oz", "owner") and float(uf["measure_factor"]) == 28.35
    ai.measure = MeasureGuess(measure="cups", factor=130)
    assert measure.of(g, ai, g.get_object("products", flour)).word == "oz" and not ai.measures  # owner's choice stays
    assert "18 oz left" in c.get("/inventory").text
    eggs = product(g, "Eggs", "piece")  # no conversion to a volume yet: said, nothing changed
    measured(g, eggs, "eggs", 1)
    r = c.post(f"/inventory/{eggs}/measure", data={"measure": "cups"})
    assert "Eggs" in r.text and "cups" in r.text and g.userfields("products", eggs)["measure"] == "eggs"


def test_inventory_use_in_cups_writes_grams(env):
    """Review Focus 3: what's typed in the measure is exactly what Grocy books in the stock unit."""
    g, _, c = env
    flour = product(g, "Flour")
    g.add_stock(flour, 1000, None, ids(g).location["Pantry"])
    measured(g, flour, "cups", 125)
    html = c.get("/inventory").text
    assert "8 cups left" in html and "<span>cups</span>" in html
    c.post(f"/pantry/{flour}/consume", data={"amount": 1.5, "measure": "cups"})
    assert g.stock_amounts()[flour] == pytest.approx(1000 - 187.5)
    c.post(f"/pantry/{flour}/amount", data={"amount": 4, "measure": "cups"})
    assert g.stock_amounts()[flour] == pytest.approx(500)
    c.post(f"/inventory/{flour}/measure", data={"measure": "tbsp"})
    tbsp = 125 * 14.79 / 236.6
    c.post(f"/pantry/{flour}/consume", data={"amount": 2, "measure": "tbsp"})
    assert g.stock_amounts()[flour] == pytest.approx(500 - 2 * tbsp)
    c.post(f"/pantry/{flour}/amount", data={"amount": 10, "measure": "tbsp"})
    assert g.stock_amounts()[flour] == pytest.approx(10 * tbsp)
    before = g.stock_amounts()[flour]
    r = c.post(f"/pantry/{flour}/consume", data={"amount": 1, "measure": "cups"})  # a page from before the change
    assert g.stock_amounts()[flour] == before and "tbsp" in r.text


def test_keep_at_least_in_rolls(env):
    g, _, c = env
    towels = supply(g, "Paper towels", min_stock=0, stock=5)
    measured(g, towels, "rolls", 1)
    html = c.get("/inventory").text
    assert "5 rolls left" in html and "<span>rolls</span>" in html
    c.post(f"/home/supplies/{towels}/min", data={"amount": 3, "measure": "rolls"})
    assert g.get_object("products", towels)["min_stock_amount"] == 3
    assert re.search(rf'id="min-{towels}-\d+"[^>]*value="3"', c.get("/inventory").text)
    soap = product(g, "Dish soap", "ml", product_group_id=g.get_object("products", towels)["product_group_id"],
                   min_stock_amount=0, location_id=ids(g).location["Under sink"])
    g.add_stock(soap, 1500, None, ids(g).location["Under sink"])
    measured(g, soap, "bottles", 750)
    c.post(f"/home/supplies/{soap}/min", data={"amount": 2, "measure": "bottles"})
    assert g.get_object("products", soap)["min_stock_amount"] == 1500
    html = c.get("/inventory").text
    assert "2 bottles left" in html and "<span>bottles</span>" in html
    assert re.search(rf'id="min-{soap}-\d+"[^>]*value="2"', html)
    c.post(f"/home/supplies/{soap}/used")  # one bottle
    assert g.stock_amounts()[soap] == 750


def test_no_grams_on_inventory_or_scan(env):
    g, ai, c = env
    i = ids(g)
    flour = product(g, "Flour")
    g.add_stock(flour, 300, None, i.location["Pantry"])
    measured(g, flour, "cups", 120)
    milk = product(g, "Milk", "ml", location_id=i.location["Fridge"])
    g.add_stock(milk, 473.2, None, i.location["Fridge"])
    measured(g, milk, "cups", 236.6)
    g.create("product_barcodes", {"product_id": milk, "barcode": "123"})
    html = c.get("/inventory").text
    assert "2 1/2 cups left" in html and "2 cups left" in html
    for gram in (" g left", " ml left", "<span>g</span>", "<span>ml</span>"):
        assert gram not in html, gram
    r = c.post("/api/scan/lookup", json={"code": "123"}).json()
    assert r["measure"] == "cups" and r["left"] == "2 cups left"
    r = c.post("/api/scan/confirm", json={"product_id": milk, "action": "add", "amount": 1, "measure": "cups"}).json()
    assert r["status"] == "ok" and g.stock_amounts()[milk] == pytest.approx(473.2 + 236.6)
    assert g.userfields("products", milk)["package_amount"] == pytest.approx(236.6)  # remembered in ml
    assert g.userfields("products", milk)["package_source"] == "owner"
    assert r["shown"] == "1 cup"
    js = c.get("/static/app.js").text
    assert "d.left" in js and "d.measure" in js and "d.unit" not in js


def test_free_mode_shows_stock_unit_until_measure(env):
    g, ai, c = env
    ai.mode_ = "free"
    ai.measure = MeasureGuess(measure="cups", factor=125)
    flour = product(g, "Flour")
    g.add_stock(flour, 500, None, ids(g).location["Pantry"])
    assert "500 g left" in c.get("/inventory").text and not ai.measures
    ai.mode_ = "full"  # Claude is back: the next showing asks once, after the page is sent
    c.get("/inventory")
    assert ai.measures == [("Flour", "g")]
    assert "4 cups left" in c.get("/inventory").text and len(ai.measures) == 1


def test_untouched_fields_leave_stock_exact(env):
    """A prefilled amount saved as it was shown (rounded to 2 decimals) writes nothing new."""
    g, _, c = env
    spinach = product(g, "Spinach")
    g.add_stock(spinach, 140, None, ids(g).location["Pantry"])
    measured(g, spinach, "cups", 30)
    html = c.get("/inventory").text
    assert re.search(rf'id="total-{spinach}-\d+"[^>]*value="4.67"', html)
    c.post(f"/pantry/{spinach}/amount", data={"amount": 4.67, "measure": "cups"})
    assert g.stock_amounts()[spinach] == 140
    c.post(f"/pantry/{spinach}/amount", data={"amount": 4.5, "measure": "cups"})  # a real change still writes
    assert g.stock_amounts()[spinach] == 135
    soap = supply(g, "Dish soap", min_stock=1000, stock=0)
    measured(g, soap, "bottles", 750)
    assert re.search(rf'id="min-{soap}-\d+"[^>]*value="1.33"', c.get("/inventory").text)
    c.post(f"/home/supplies/{soap}/min", data={"amount": 1.33, "measure": "bottles"})
    assert g.get_object("products", soap)["min_stock_amount"] == 1000


def test_scan_min_and_supply_use_in_a_measure(env):
    """Scan: a new supply's keep-at-least and using supplies, for a measure that isn't one stock unit each."""
    g, ai, c = env
    soap = supply(g, "Dish soap", min_stock=0, stock=3000, package_amount=1000)
    measured(g, soap, "bottles", 750)
    g.create("product_barcodes", {"product_id": soap, "barcode": "321"})
    r = c.post("/api/scan/lookup", json={"code": "321"}).json()
    assert (r["measure"], r["left"], r["keep"]) == ("bottles", "4 bottles left", 1.33)
    ok = c.post("/api/scan/confirm", json={"product_id": soap, "action": "add", "min": 1.33, "measure": "bottles"})
    assert ok.json()["status"] == "ok" and g.get_object("products", soap)["min_stock_amount"] == 1000  # untouched
    c.post("/api/scan/confirm", json={"product_id": soap, "action": "add", "min": 2, "measure": "bottles"})
    assert g.get_object("products", soap)["min_stock_amount"] == 1500
    r = c.post("/api/scan/confirm", json={"product_id": soap, "action": "use", "packs": 2, "measure": "bottles"}).json()
    assert r["amount"] == 1500 and r["shown"] == "2 bottles" and not r["short"]
    r = c.post("/api/scan/confirm", json={"product_id": soap, "action": "add", "amount": 1})  # no measure said
    assert r.status_code == 400 and "bottles" in r.json()["message"]


def test_measure_in_needs_a_pick(env):
    g, _, c = env
    flour = product(g, "Flour")
    g.add_stock(flour, 500, None, ids(g).location["Pantry"])
    assert re.search(r'<select id="measure-\d+-\d+" name="measure" required>', c.get("/inventory").text)
    r = c.post(f"/inventory/{flour}/measure", data={}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_count_word_stays_a_choice_after_a_change(env):
    g, _, c = env
    soap = supply(g, "Dish soap", min_stock=0, stock=1500)
    g.update("products", soap, {"qu_id_stock": ids(g).unit["ml"]})
    measured(g, soap, "bottles", 750)
    c.post(f"/inventory/{soap}/measure", data={"measure": "cups"})
    p = g.get_object("products", soap)
    m = measure.of(g, None, p)
    assert m.word == "cups" and "bottles" in measure.choices("ml", m)
    assert measure.change(g, p, "bottles") == Measure("bottles", 750)
    assert "2 bottles left" in c.get("/inventory").text


def test_singular_words():
    for many, one in (("loaves", "loaf"), ("cookies", "cookie"), ("boxes", "box"), ("cans", "can"),
                      ("berries", "berry"), ("olives", "olive"), ("cups", "cup"), ("tbsp", "tbsp")):
        assert measure.show(1, Measure(many, 1)) == f"1 {one}", many


def test_background_fill_asks_a_failed_product_once_a_day(env):
    g, ai, c = env
    flour = product(g, "Flour")
    g.add_stock(flour, 500, None, ids(g).location["Pantry"])
    ai.measure = MeasureGuess(measure="g", factor=1)  # not a measure: refused by fixed()
    c.get("/inventory")
    c.get("/inventory")
    assert ai.measures == [("Flour", "g")]


def test_brand_food_row_asks_a_measure_for_its_generic_too(env):
    g, ai, c = env
    milk = product(g, "Milk", unit="ml")
    brand = product(g, "Store milk", unit="ml", parent_product_id=milk)
    g.add_stock(brand, 500, None, ids(g).location["Pantry"])
    ai.measure = MeasureGuess(measure="cups", factor=236.6)
    c.get("/inventory")
    assert {n for n, _ in ai.measures} == {"Milk", "Store milk"}
    assert g.userfields("products", milk)["measure"] == "cups"


def test_measure_in_on_a_brand_row_changes_its_generic_too(env):
    g, ai, c = env
    milk = product(g, "Milk", unit="ml")
    brand = product(g, "Store milk", unit="ml", parent_product_id=milk)
    g.add_stock(brand, 500, None, ids(g).location["Pantry"])
    measured(g, milk, "cups", 236.6)
    measured(g, brand, "cups", 236.6)
    c.post(f"/inventory/{brand}/measure", data={"measure": "tbsp"})
    assert g.userfields("products", brand)["measure"] == g.userfields("products", milk)["measure"] == "tbsp"


def test_shopping_asks_a_measure_for_products_without_one(env):
    g, ai, c = env
    supply(g, "Dish soap", min_stock=2, stock=0)
    ai.measure = MeasureGuess(measure="bottles", factor=750)
    c.get("/shopping")
    assert [n for n, _ in ai.measures] == ["Dish soap"]
