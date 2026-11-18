from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import ConversionGuess, Picks, ProductGuess, RecipeDraft
from app.cfe import CfeTable, Ingredient, Op
from app.config import Settings
from app.grocy import GrocyError
from app.importer import IngredientMatch, PendingStore, save_recipe
from app.setup_grocy import bootstrap, ids
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy
from tests.test_cfe import BROWNIE

SOUP_PAGE = '<script type="application/ld+json">{"@type":"Recipe","name":"Soup"}</script>'


def no_network(req):
    if req.url.host == "example.com":
        return httpx.Response(200, text=SOUP_PAGE)
    return httpx.Response(404)


@pytest.fixture
def env(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    ai = FakeAI(picks=Picks(items=[]))
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    store = PendingStore(tmp_path / "pending")
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(no_network)), settings, store)
    return g, ai, store, TestClient(app), tmp_path


def product(g, name, unit="g", **kw):
    i = ids(g)
    return g.create("products", {"name": name, "qu_id_stock": i.unit[unit], "location_id": i.location["Pantry"],
                                 "default_best_before_days": 5, **kw})


def brownie_recipe(g):
    pids = [product(g, ing.name) for ing in BROWNIE.ingredients]
    draft = RecipeDraft(title="Brownies", servings=2, steps=["Mix", "Bake"], table=BROWNIE)
    return save_recipe(g, draft, [IngredientMatch(i, p, 10) for i, p in enumerate(pids)], None, None)


def soup_draft():
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"),
                                      Ingredient(qty=1, unit="", name="dragon egg")],
                 ops=[Op(id="o1", label="mix", inputs=["i0", "i1"])])
    return RecipeDraft(title="Soup", servings=4, steps=["Stir"], table=t)


def test_root_redirects_to_plan(env):
    r = env[3].get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and r.headers["location"] == "/plan"


def test_pages_render(env):
    g, ai, store, c, _ = env
    rid = brownie_recipe(g)
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    for url in ["/pantry", "/scan", "/recipes", f"/recipes/{rid}", f"/recipes/{rid}/table", "/import",
                f"/import/{pid}", "/static/style.css", "/static/app.js", "/static/manifest.webmanifest"]:
        assert c.get(url).status_code == 200, url
    assert c.get("/recipes/999").status_code == 404 and c.get("/import/" + "0" * 32).status_code == 404


def test_base_has_pwa_bits_and_nav(env):
    html = env[3].get("/pantry").text
    for bit in ['name="viewport"', 'rel="manifest"', 'apple-touch-icon', 'apple-mobile-web-app-capable" content="yes"',
                'href="/plan"', 'href="/pantry"', 'href="/scan"', 'href="/recipes"', 'href="/shopping"']:
        assert bit in html, bit


def test_recipe_page_has_cfe_table_and_wakelock(env):
    g, _, _, c, _ = env
    html = c.get(f"/recipes/{brownie_recipe(g)}").text
    assert 'rowspan="9"' in html and "data-wakelock" in html and "Preheat oven" in html
    assert "macros incomplete" in html and "unsalted butter" in html and 'href="/recipes/' in html


def test_recipe_servings_scales(env):
    g, _, _, c, _ = env
    html = c.get(f"/recipes/{brownie_recipe(g)}?servings=4").text
    assert "8 oz unsalted butter" in html and 'value="4"' in html


def test_recipe_macros_per_serving(env):
    g, _, _, c, _ = env
    flour = product(g, "Flour")
    g.set_userfields("products", flour, {"kcal_100": 364, "protein_100": 10, "carbs_100": 76, "fat_100": 1, "g_per_unit": 1})
    rid = g.create("recipes", {"name": "Dough", "base_servings": 2, "description": "<ol><li>Knead</li></ol>"})
    g.create("recipes_pos", {"recipe_id": rid, "product_id": flour, "amount": 200, "qu_id": ids(g).unit["g"]})
    html = c.get(f"/recipes/{rid}").text
    assert "364 kcal" in html and "macros incomplete" not in html
    # no cfe_table userfield: fallback list + steps + edit link
    assert "200 g Flour" in html and "<li>Knead</li>" in html and f'href="/recipes/{rid}/table"' in html
    assert 'class="cfe"' not in html


def test_recipe_invalid_table_falls_back(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    bad = BROWNIE.model_copy(update={"ops": [Op(id="o1", label="mix", inputs=[])]})
    g.set_userfields("recipes", rid, {"cfe_table": bad.model_dump_json()})
    html = c.get(f"/recipes/{rid}").text
    assert 'class="cfe"' not in html and "<li>Mix</li>" in html and "Edit table" in html


def test_html_is_autoescaped(env):
    g, _, _, c, _ = env
    g.create("recipes", {"name": "<b>Bold</b>", "base_servings": 2})
    assert "&lt;b&gt;Bold" in c.get("/recipes").text


def test_recipes_list_hides_grocy_internal_recipes(env):
    g, _, _, c, _ = env
    g.create("recipes", {"name": "Chili", "base_servings": 2, "type": "normal"})
    g.create("recipes", {"name": "2026-11-19", "base_servings": 1, "type": "mealplan-day"})
    html = c.get("/recipes").text
    assert "Chili" in html and "2026-11-19" not in html


def test_add_to_plan(env):
    g, _, _, c, _ = env
    rid = g.create("recipes", {"name": "Chili", "base_servings": 2})
    r = c.post(f"/recipes/{rid}/plan", data={"day": "2026-11-20", "section": "Dinner", "servings": ""},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/plan"
    [entry] = g.objects("meal_plan", type="recipe")
    assert entry["recipe_id"] == rid and entry["recipe_servings"] == 2  # Friday dinner default


def test_scan_known_add_increments_stock(env):
    g, _, _, c, _ = env
    i = ids(g)
    generic = product(g, "Milk", "ml")
    pid = product(g, "Brand Milk", "ml", parent_product_id=generic, location_id=i.location["Fridge"],
                  default_best_before_days=7)
    g.set_userfields("products", pid, {"package_amount": 946})
    g.create("product_barcodes", {"product_id": pid, "barcode": "123"})
    r = c.post("/api/scan", json={"code": "123", "action": "add"}).json()
    assert r == {"status": "ok", "product": "Brand Milk", "generic": "Milk", "created": False, "product_id": pid,
                 "action": "add", "amount": 946}
    [e] = g.stock[pid]
    assert e["amount"] == 946 and e["location_id"] == i.location["Fridge"]
    assert e["best_before_date"] == (date.today() + timedelta(days=7)).isoformat()
    assert c.post("/api/scan", json={"code": "123", "action": "use"}).json()["amount"] == 946
    assert g.stock[pid] == []
    assert c.post("/api/scan", json={"code": "123", "action": "use"}).json()["amount"] == 0  # nothing left


def test_scan_unknown_returns_unknown(env):
    assert env[3].post("/api/scan", json={"code": "999", "action": "add"}).json() == {"status": "unknown"}


def test_scan_photo_creates_and_adds(env):
    g, ai, _, c, _ = env
    ai.guess = ProductGuess(generic_name="Beans", is_new_generic=True, stock_unit="g", package_amount=400,
                            location="Pantry", best_before_days=700, g_per_unit=1)
    r = c.post("/api/scan/photo", data={"code": "555", "action": "add"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "ok" and r["created"] and r["generic"] == "Beans" and r["amount"] == 400
    assert g.product_by_barcode("555")["product"]["id"] == r["product_id"]
    assert g.stock[r["product_id"]][0]["amount"] == 400


def test_relink(env):
    g, _, _, c, _ = env
    old = product(g, "Pasta")
    child = product(g, "Brand Pasta", parent_product_id=old)
    assert c.post("/api/relink", json={"product_id": child, "generic_name": "Spaghetti"}).json() == {"status": "ok"}
    parent = g.get_object("products", g.get_object("products", child)["parent_product_id"])
    assert parent["name"] == "Spaghetti"
    r = c.post("/api/relink", json={"product_id": child, "generic_name": "Brand Pasta"})
    assert r.status_code == 400 and "child product" in r.json()["message"]


def test_pantry_use_soon_and_actions(env):
    g, _, _, c, _ = env
    i = ids(g)
    pid = product(g, "Yogurt")
    g.add_stock(pid, 500, date(2026, 11, 20), i.location["Fridge"])
    g.volatile_result["due_products"] = [{"product_id": pid, "amount": 500, "best_before_date": "2026-11-20"}]
    html = c.get("/pantry").text
    assert html.index("Use soon") < html.index("Fridge") and html.count("Yogurt") >= 2
    assert c.post(f"/pantry/{pid}/consume", data={"amount": 100}, follow_redirects=False).headers["location"] == "/pantry"
    assert g.stock[pid][0]["amount"] == 400
    c.post(f"/pantry/{pid}/move", data={"from_location": i.location["Fridge"], "to_location": i.location["Freezer"],
                                        "amount": 400})
    assert g.stock[pid][0]["location_id"] == i.location["Freezer"]
    c.post(f"/pantry/{pid}/amount", data={"amount": 250})
    assert sum(e["amount"] for e in g.stock[pid]) == 250


def review_form(**extra):
    base = {"product_0": "", "amount_0": "", "prev_0": "", "factor_0": "", "new_name_0": "flour",
            "product_1": "", "amount_1": "", "prev_1": "", "factor_1": "", "new_name_1": "dragon egg",
            "op_id": ["o1", "o2"], "op_label": ["mix", ""], "op_inputs": ["i0, i1", ""], "action": "save"}
    return {**base, **extra}


def test_import_creates_pending_and_review_save(env):
    g, ai, store, c, tmp = env
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.draft = soup_draft()
    r = c.post("/import", data={"urls": "https://example.com/soup\n\n"},
               files=[("files", ("soup.pdf", b"%PDF-1", "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303
    pend = store.all()
    assert len(pend) == 2 and all(p.status == "ready" for p in pend)
    pdf = next(p for p in pend if p.source == "soup.pdf")
    html = c.get(f"/import/{pdf.id}").text
    assert "2 cup flour" in html and "dragon egg" in html and "1 cup =" in html  # flour g, no cup conversion
    r = c.post(f"/import/{pdf.id}", data=review_form(product_0=flour, prev_0=flour, factor_0="125",
                                                    product_1=egg, prev_1=egg, amount_1="1"),
               follow_redirects=False)
    assert r.status_code == 303
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"]) for p in pos] == [(flour, 250), (egg, 1)]
    assert store.get(pdf.id) is None and store.pdf_path(pdf.id) is None
    assert (tmp / "sources" / f"{rid}.pdf").read_bytes() == b"%PDF-1"
    url = next(p for p in pend if p.source.startswith("https"))
    assert c.post(f"/import/{url.id}/discard", follow_redirects=False).status_code == 303
    assert store.all() == []


def test_review_create_suggest_and_errors(env):
    g, ai, store, c, _ = env
    flour = product(g, "Flour")
    ai.draft = soup_draft()
    ai.guess = ProductGuess(generic_name="x", is_new_generic=True, stock_unit="piece", package_amount=None,
                            location="Pantry", best_before_days=30, g_per_unit=60)
    ai.conversion = ConversionGuess(factor=125, explanation="a cup of flour weighs about 125 g")
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    html = c.post(f"/import/{pid}", data=review_form(action="create:1", new_name_1="Dragon egg")).text
    egg = next(p["id"] for p in g.objects("products") if p["name"] == "Dragon egg")
    assert f'<option value="{egg}" selected' in html and 'name="amount_1" value="1"' in html
    html = c.post(f"/import/{pid}", data=review_form(action="suggest:0", product_0=flour)).text
    assert 'name="factor_0" value="125' in html and "about 125 g" in html
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, factor_0="abc")).text
    assert "could not convert" in html and store.get(pid) is not None
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, factor_0="inf")).text
    assert "finite" in html
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, factor_0="125", product_1=egg, prev_1=egg,
                                                     amount_1="1", op_inputs=["i0", ""])).text
    assert "i1 is not used by any op" in html and g.objects("recipes") == []
    assert 'name="amount_0" value="250' in html  # conversion was applied and kept


def test_table_edit_rejects_invalid_and_saves_valid(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    before = g.userfields("recipes", rid)["cfe_table"]
    ids_ = [o.id for o in BROWNIE.ops]
    labels = [o.label for o in BROWNIE.ops]
    inputs = [", ".join(o.inputs) for o in BROWNIE.ops]
    bad = inputs.copy()
    bad[1] = "o1, i2, i3"  # i1 unused
    r = c.post(f"/recipes/{rid}/table", data={"op_id": ids_, "op_label": labels, "op_inputs": bad})
    assert r.status_code == 200 and "i1 is not used by any op" in r.text
    assert g.userfields("recipes", rid)["cfe_table"] == before
    labels[0] = "brown"
    r = c.post(f"/recipes/{rid}/table", data={"op_id": ids_ + ["o6"], "op_label": labels + [""],
                                              "op_inputs": inputs + [""]}, follow_redirects=False)
    assert r.status_code == 303
    saved = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    assert saved.ops[0].label == "brown" and len(saved.ops) == 5 and saved.ingredients == BROWNIE.ingredients


class DownGrocy(FakeGrocy):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def objects(self, entity, **where):
        raise self.exc


@pytest.mark.parametrize("exc", [GrocyError("GET /objects/recipes -> 502"), httpx.ConnectError("refused")])
def test_grocy_down_shows_503(tmp_path, exc):
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    app = create_app(DownGrocy(exc), FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)), settings,
                     PendingStore(tmp_path / "pending"))
    r = TestClient(app).get("/recipes")
    assert r.status_code == 503 and "Can't reach Grocy — nothing was saved. Try again in a minute." in r.text
