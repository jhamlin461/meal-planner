import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import ConversionGuess, IngredientPick, Picks, ProductGuess, RecipeDraft
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


def chips(t: CfeTable) -> dict:
    """The table editor's form for a table: one label per step and its ticked chips."""
    return {"op_id": [o.id for o in t.ops], "op_label": [o.label for o in t.ops],
            **{f"op_in_{k}": list(o.inputs) for k, o in enumerate(t.ops)}}


def soup_draft():
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"),
                                      Ingredient(qty=1, unit="", name="dragon egg")],
                 ops=[Op(id="o1", label="mix", inputs=["i0", "i1"])])
    return RecipeDraft(title="Soup", servings=4, steps=["Stir"], table=t)


def test_root_renders_today_in_shell(env):
    r = env[3].get("/", cookies={"who": "kitchen"}, follow_redirects=False)
    assert r.status_code == 200 and "<h1" in r.text and 'class="rail"' in r.text
    assert 'href="/" aria-current="page"' in r.text


def _tabs(html):
    nav = html[html.index('class="rail"'):html.index("</nav>")]
    return re.findall(r'<li><a href="([^"]+)"[^>]*>.*?<span>([^<]+)</span>', nav)


def _switcher(html, label):
    nav = html[html.index(f'<nav class="segmented pages" aria-label="{label}"'):]
    return re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', nav[:nav.index("</nav>")])


def test_tabs_are_today_meals_chores_me(env):
    html = env[3].get("/pantry").text
    assert _tabs(html) == [("/", "Today"), ("/plan", "Meals"), ("/home", "Chores"), ("/me", "Me")]
    assert 'href="/plan" aria-current="page"' in html
    assert env[3].get("/home").status_code == 200


def test_meals_switcher_has_four_pages(env):
    html = env[3].get("/pantry").text
    assert [(h, t) for h, _, t in _switcher(html, "Meals pages")] == [("/plan", "Plan"), ("/pantry", "Pantry"),
                                                                     ("/recipes", "Recipes"), ("/shopping", "Shopping")]
    assert [t for h, a, t in _switcher(html, "Meals pages") if 'aria-current="page"' in a] == ["Pantry"]
    assert 'href="/plan" aria-current="page"' in html and "subnav" not in html and "has-subnav" not in html


def test_chores_switcher_has_two_pages(env):
    c = env[3]
    for url, current in [("/home", "Chores"), ("/home/supplies", "Supplies")]:
        html = c.get(url).text
        assert [(h, t) for h, _, t in _switcher(html, "Chores pages")] == [("/home", "Chores"),
                                                                          ("/home/supplies", "Supplies")], url
        assert [t for h, a, t in _switcher(html, "Chores pages") if "aria-current" in a] == [current], url
        assert 'href="/home" aria-current="page"' in html, url
    assert "Supplies you keep at home will show here." in c.get("/home/supplies").text


def test_scan_button_only_on_inventory_pages(env):
    c = env[3]
    row = re.compile(r'<div class="title-row"><h1[^>]*>.*?</h1>\s*<a class="button title-action" href="([^"]+)">'
                     r'.*?barcode-scan.*?Scan</a></div>', re.S)
    for url, href in [("/pantry", "/scan"), ("/home/supplies", "/scan?from=chores")]:
        html = c.get(url).text
        assert row.findall(html) == [href] and html.count("<h1") == 1, url
    for url in ["/plan", "/recipes", "/shopping", "/import", "/home", "/home/chores/new", "/scan", "/scan?from=chores"]:
        html = c.get(url).text
        assert "title-action" not in html and html.count("<h1") == 1, url
    c.cookies.set("who", "kitchen")
    for url in ["/", "/me"]:
        html = c.get(url).text
        assert "title-action" not in html and html.count("<h1") == 1, url
    for url in ["/plan", "/home", "/static/style.css", "/static/app.js"]:
        text = c.get(url).text
        assert ".fab" not in text and 'class="fab"' not in text and '"away"' not in text, url


def test_rail_has_no_scan_entries(env):
    c = env[3]
    for url in ["/plan", "/pantry", "/home", "/home/supplies", "/scan", "/scan?from=chores"]:
        rail = c.get(url).text
        rail = rail[rail.index('class="rail"'):rail.index("</nav>")]
        assert "/scan" not in rail and "barcode-scan" not in rail, url


def test_scan_page_keeps_section_switcher(env):
    c = env[3]
    html = c.get("/scan?from=chores").text
    assert [t for _, _, t in _switcher(html, "Chores pages")] == ["Chores", "Supplies"]
    assert not [t for _, a, t in _switcher(html, "Chores pages") if "aria-current" in a]
    assert 'aria-label="Meals pages"' not in html and 'href="/home" aria-current="page"' in html
    html = c.get("/scan").text
    assert [t for _, _, t in _switcher(html, "Meals pages")] == ["Plan", "Pantry", "Recipes", "Shopping"]
    assert not [t for _, a, t in _switcher(html, "Meals pages") if "aria-current" in a]


def _render_base(path, query=""):
    from types import SimpleNamespace
    from urllib.parse import parse_qsl
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(Path(__file__).parent.parent / "app/templates"),
                             autoescape=True)
    req = SimpleNamespace(url=SimpleNamespace(path=path), query_params=dict(parse_qsl(query)), cookies={})
    return env.get_template("base.html").render(request=req)


def test_switcher_matches_whole_path_segments():
    def current(path, label="Meals pages"):
        return [t for _, a, t in _switcher(_render_base(path), label) if "aria-current" in a]
    assert current("/plan") == ["Plan"] and current("/recipes/7") == ["Recipes"]
    assert current("/plans") == [] and current("/shoppinglist") == []
    assert current("/home", "Chores pages") == ["Chores"] and current("/home/supplies", "Chores pages") == ["Supplies"]


def test_me_page_theme_and_device(env):
    c = env[3]
    c.cookies.set("who", "kitchen")
    html = c.get("/me").text
    assert 'href="/me" aria-current="page"' in html and 'href="/who"' in html and 'href="/home/people"' in html
    assert "kitchen screen" in html.lower() and 'class="rows"' in html and "Save theme" not in html
    assert re.search(r'value="dark"[^>]*aria-pressed="true"', html) and re.search(r'value="light"[^>]*aria-pressed="false"', html)
    r = c.post("/settings/theme", data={"theme": "light"}, headers={"referer": "http://testserver/me"},
               follow_redirects=False)
    assert r.headers["location"] == "/me"
    html = c.get("/me").text
    assert 'data-theme="light"' in html and re.search(r'value="light"[^>]*aria-pressed="true"', html)
    assert 'href="/me" aria-current="page"' in c.get("/who").text
    assert 'href="/me" aria-current="page"' in c.get("/home/people").text


def test_chores_page_sections(env):
    html = env[3].get("/home").text
    assert "<h1>Chores</h1>" in html and "<h2>Repeating</h2>" in html and "<h2>Chores</h2>" not in html


def test_no_footer_theme_button(env):
    for url in ["/", "/pantry", "/home"]:
        assert "theme-switch" not in env[3].get(url).text, url


def test_theme_cookie_switches_to_light(env):
    c = env[3]
    assert 'data-theme="dark"' in c.get("/pantry").text
    r = c.post("/settings/theme", data={"theme": "light"}, headers={"referer": "http://testserver/pantry"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pantry"
    assert "max-age=34560000" in r.headers["set-cookie"].lower()
    assert 'data-theme="light"' in c.get("/pantry").text
    assert c.post("/settings/theme", data={"theme": "pink"}).status_code == 422
    r = c.post("/settings/theme", data={"theme": "dark"}, headers={"referer": "http://evil.example//evil.example/x"},
               follow_redirects=False)
    assert r.headers["location"] == "/"


def test_self_hosted_assets_served(env):
    c = env[3]
    assert c.get("/static/icons.svg").status_code == 200
    assert c.get("/static/fonts/RobotoFlex.woff2").status_code == 200
    assert 'id="silverware-fork-knife"' in c.get("/static/icons.svg").text
    base = (Path(__file__).parent.parent / "app/templates/base.html").read_text()
    css = c.get("/static/style.css").text
    assert "http://" not in base + css and "https://" not in base + css


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
    assert 'class="cfe-wrap"' in html
    assert 'rowspan="9"' in html and "data-wakelock" in html and "Preheat oven" in html
    assert "these figures run low" in html and "unsalted butter" in html and 'href="/recipes/' in html


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
    assert "364 kcal" in html and "these figures run low" not in html
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


def test_plan_marks_today_and_links_recipe(env):
    import datetime
    g, _, _, c, _ = env
    rid = g.create("recipes", {"name": "Chili", "base_servings": 2})
    c.post(f"/recipes/{rid}/plan", data={"day": datetime.date.today().isoformat(), "section": "Dinner", "servings": ""})
    html = c.get("/plan").text
    assert "is-today" in html and f'href="/recipes/{rid}"' in html


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
            "op_id": ["o1", ""], "op_label": ["mix", ""], "op_in_0": ["i0", "i1"], "action": "save"}
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
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, prev_0=flour, factor_0="abc")).text
    assert "could not convert" in html and store.get(pid) is not None
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, prev_0=flour, factor_0="inf")).text
    assert "finite" in html
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, prev_0=flour, factor_0="125", product_1=egg, prev_1=egg,
                                                     amount_1="1", op_in_0=["i0"])).text
    assert "“dragon egg” isn&#39;t in any step yet" in html and g.objects("recipes") == []
    assert 'name="amount_0" value="250' in html  # conversion was applied and kept


def test_table_edit_rejects_invalid_and_saves_valid(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    before = g.userfields("recipes", rid)["cfe_table"]
    r = c.post(f"/recipes/{rid}/table", data={**chips(BROWNIE), "op_in_1": ["o1", "i2", "i3"]})  # sugar unused
    assert r.status_code == 200 and "“sugar” isn&#39;t in any step yet" in r.text
    assert g.userfields("recipes", rid)["cfe_table"] == before
    form = chips(BROWNIE)
    form["op_label"][0] = "brown"
    r = c.post(f"/recipes/{rid}/table", data={**form, "op_id": form["op_id"] + [""], "op_label": form["op_label"] + [""]},
               follow_redirects=False)
    assert r.status_code == 303
    saved = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    assert saved.ops[0].label == "brown" and len(saved.ops) == 5 and saved.ingredients == BROWNIE.ingredients


def test_review_shows_no_internal_ids(env):
    g, ai, store, c, _ = env
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"), Ingredient(qty=1, unit="", name="egg")],
                 ops=[Op(id="r1", label="whisk", inputs=["i0"]), Op(id="a1", label="fold in", inputs=["r1", "i1"])])
    ai.draft = RecipeDraft(title="Cake", servings=2, steps=["Mix"], table=t)
    c.post("/import", data={"urls": "https://example.com/cake"})
    pages = [c.get(f"/import/{store.all()[0].id}").text, c.get(f"/recipes/{brownie_recipe(g)}/table").text]
    for html in pages:
        visible = re.sub(r"<[^>]*>", " ", html)  # text only; ids may live in values, never on screen
        assert not re.search(r"\b[iora]\d+\b", visible), re.findall(r".{20}\b[iora]\d+\b.{20}", visible)
        assert not re.search(r'(aria-label|placeholder|title)="[^"]*\b[iora]\d+\b', html)
        assert "o1, i2" not in html and "Combines" in html and "Remove step" in html
        assert "Each step combines ingredients and earlier steps. Tap what goes into it." in html
    assert re.search(r'value="i1"[^>]*>\s*<span>egg</span>', pages[0])
    assert re.search(r'value="r1"[^>]*>\s*<span>whisk</span>', pages[0])  # earlier step chip, by its label


def test_combines_chips_round_trip(env):
    g, ai, store, c, _ = env
    rid = brownie_recipe(g)
    form = chips(BROWNIE)
    del form["op_in_4"]  # unticked chips are simply not posted: bake has none, so it is invalid
    assert "“bake 350°F 30 to 40 min” doesn&#39;t combine anything yet" in c.post(f"/recipes/{rid}/table", data=form).text
    r = c.post(f"/recipes/{rid}/table", data=chips(BROWNIE), follow_redirects=False)
    assert r.status_code == 303
    assert CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ops == BROWNIE.ops
    # AI ids (r1, a1) are accepted and renumbered o1, o2 in row order
    t = soup_draft().table.model_copy(update={"ops": [Op(id="r1", label="beat", inputs=["i1"]),
                                                      Op(id="a1", label="mix", inputs=["r1", "i0"])]})
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.draft = RecipeDraft(title="Soup", servings=4, steps=["Stir"], table=t)
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    r = c.post(f"/import/{pid}", data={**review_form(product_0=flour, prev_0=flour, factor_0="125", product_1=egg,
                                                     prev_1=egg, amount_1="1"), **chips(t)}, follow_redirects=False)
    assert r.status_code == 303
    saved = CfeTable.model_validate_json(g.userfields("recipes", int(r.headers["location"].rsplit("/", 1)[1]))["cfe_table"])
    assert [(o.id, o.label, o.inputs) for o in saved.ops] == [("o1", "beat", ["i1"]), ("o2", "mix", ["o1", "i0"])]


def test_duplicate_names_show_amounts(env):
    g, _, _, c, _ = env
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="tsp", name="kosher salt"),
                                      Ingredient(qty=None, unit="", name="kosher salt", note="to taste"),
                                      Ingredient(qty=1, unit="cup", name="rice")],
                 ops=[Op(id="o1", label="season", inputs=["i0", "i1", "i2"])])
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}/table").text  # brownies mix twice: earlier steps tell them apart by position
    assert "<span>mix, step 2</span>" in html and "<span>mix, step 3</span>" in html and "<span>melt</span>" in html
    g.set_userfields("recipes", rid, {"cfe_table": t.model_dump_json()})
    html = c.get(f"/recipes/{rid}/table").text
    assert "<span>kosher salt, 1 tsp</span>" in html and "<span>kosher salt, to taste</span>" in html
    assert "<span>rice</span>" in html


def test_remove_step_remaps_references(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    before = g.userfields("recipes", rid)["cfe_table"]
    html = c.post(f"/recipes/{rid}/table", data={**chips(BROWNIE), "action": "remove:0"}).text  # remove "melt"
    assert g.userfields("recipes", rid)["cfe_table"] == before  # removing only re-renders
    assert 'value="melt"' not in html and html.count("Remove step") == 4
    form = dict(re.findall(r'name="(op_in_\d+)" value="(o\d+)" checked', html))
    assert form == {"op_in_1": "o1", "op_in_2": "o2", "op_in_3": "o3"}  # mix(o2) took o1; nothing points at melt
    assert re.findall(r'name="op_id" value="([^"]*)"', html) == ["o1", "o2", "o3", "o4", ""]
    # blanking a step (no label, no ticks) removes it the same way: ticks on it go too, no unknown-step error
    html = c.post(f"/recipes/{rid}/table", data={**chips(BROWNIE), "op_label": ["", *chips(BROWNIE)["op_label"][1:]],
                                                 "op_in_0": []}).text
    assert "isn&#39;t an ingredient or an earlier step" not in html and "“unsalted butter” isn&#39;t in any step" in html
    assert re.findall(r'name="op_id" value="([^"]*)"', html) == ["o1", "o2", "o3", "o4", ""]


def test_unknown_input_id_rejected(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    before = g.userfields("recipes", rid)["cfe_table"]
    for bad in (["o1", "i1", "i2", "i3", "zz"], ["o1", "i1", "i2", "i3", "o3"], ["o2", "o1", "i1", "i2", "i3"]):
        r = c.post(f"/recipes/{rid}/table", data={**chips(BROWNIE), "op_in_1": bad})  # unknown, later, itself
        assert r.status_code == 200 and "combines something that isn&#39;t an ingredient or an earlier step" in r.text
    assert g.userfields("recipes", rid)["cfe_table"] == before


def _shown(html: str) -> list[set[str]]:
    """Per step row, the chips shown without "Show all": those not marked used by another step."""
    rows = html.split('<li class="step-row">')[1:]
    return [set(re.findall(r'<label class="chip(?: chip-step)?"[^>]*><input type="checkbox" name="op_in_\d+" value="([^"]+)"', r))
            for r in rows]


def test_editor_shows_only_unused_chips(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}/table").text
    assert _shown(html) == [{"i0"}, {"o1", "i1", "i2", "i3"}, {"o2", "i4"}, {"o3", "i5", "i6", "i7", "i8"}, {"o4"}, {"o5"}]  # a new step can take bake
    assert html.count('class="chip used"') + html.count('class="chip chip-step used"') > 0
    assert html.count('class="show-all"') == 6  # one per step row; CSS hides it when nothing is tucked away
    assert 'aria-label="Show all for melt"' in html and 'aria-label="Show all for the new step"' in html
    # unticking bake's input puts fold in (o4) back on show for bake and the new step; bake itself is unused too
    html = c.post(f"/recipes/{rid}/table", data={**chips(BROWNIE), "op_in_4": [], "action": "addop"}).text
    assert _shown(html)[4] == {"o4"} and _shown(html)[5] == {"o4", "o5"}


def test_plain_language_errors(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    url = f"/recipes/{rid}/table"
    cases = [
        ({"op_in_3": ["i5", "i6", "i7", "i8"]},  # second mix feeds nothing: two last steps
         "Every step except the last must feed into a later step; “mix, step 3” doesn't."),
        ({"op_in_0": ["i0", "i2"], "op_in_1": ["o1", "i1", "i3"]},
         "“melt” combines ingredients that aren't next to each other in the list. "
         "Tick neighbouring ones, or split it into more steps."),
        ({"op_in_1": ["o1", "i0", "i1", "i2", "i3"]}, "“unsalted butter” is ticked in 2 steps; each ingredient goes into just one."),
        ({"op_in_2": ["o2", "o1", "i4"]}, "“melt” feeds into 2 steps; each step goes into just one."),
        ({"op_id": [""], "op_label": [""]}, "Add at least one step."),
    ]
    for change, msg in cases:
        html = c.post(url, data=change if "op_id" in change else {**chips(BROWNIE), **change}).text
        errs = re.findall(r'<p class="card error" role="alert">([^<]*)</p>', html)
        assert msg.replace("'", "&#39;") in errs, errs
        assert not any(re.search(r"\b(op|ops|root|rows|i\d+|o\d+)\b", e) for e in errs), errs


class DownGrocy(FakeGrocy):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def objects(self, entity, **where):
        raise self.exc


@pytest.mark.parametrize("exc", [GrocyError("GET /objects/recipes -> 502"), httpx.ConnectError("refused"),
                                 httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
def test_grocy_down_shows_503(tmp_path, exc):
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    app = create_app(DownGrocy(exc), FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)), settings,
                     PendingStore(tmp_path / "pending"))
    r = TestClient(app).get("/recipes")
    assert r.status_code == 503 and "may not have finished — check before trying again" in r.text
    assert "nothing was saved" not in r.text  # Grocy can fail midway through a multi-write action


def test_review_packet_row_asks_for_amount_not_factor(env):
    g, ai, store, c, _ = env
    sour = product(g, "Sour cream")
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="packet", name="sour cream")],
                 ops=[Op(id="o1", label="mix", inputs=["i0"])])
    ai.draft = RecipeDraft(title="Tacos", servings=2, steps=["Stir"], table=t)
    c.post("/import", data={"urls": "https://example.com/tacos"})
    pid = store.all()[0].id
    form = dict(product_0=sour, prev_0=sour, amount_0="", factor_0="", new_name_0="sour cream", op_id=["o1"],
                op_label=["mix"], op_in_0=["i0"])
    html = c.post(f"/import/{pid}", data={**form, "action": "update"}).text
    assert 'name="amount_0"' in html and "factor_0" not in html and "Suggest" not in html
    assert "Packet sizes vary — enter the real amount" in html
    # a posted factor for a packet row is ignored: no error, no conversion row
    html = c.post(f"/import/{pid}", data={**form, "action": "update", "factor_0": "30"}).text
    assert 'class="error"' not in html and not g.objects("quantity_unit_conversions", product_id=sour)
    r = c.post(f"/import/{pid}", data={**form, "action": "save", "amount_0": "30", "factor_0": "30"},
               follow_redirects=False)
    assert r.status_code == 303
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    assert [p["amount"] for p in g.objects("recipes_pos", recipe_id=rid)] == [30]
    assert not g.objects("quantity_unit_conversions", product_id=sour)


def test_review_factor_is_not_applied_to_a_newly_picked_product(env):
    g, ai, store, c, _ = env
    flour, sugar = product(g, "Flour"), product(g, "Sugar")
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    html = c.post(f"/import/{pid}", data=review_form(action="update", product_0=flour, prev_0=flour,
                                                     factor_0="125")).text
    assert 'name="amount_0" value="250"' in html
    # reviewer switches the row to Sugar; the hidden factor typed for Flour comes along with the form
    html = c.post(f"/import/{pid}", data=review_form(action="update", product_0=sugar, prev_0=flour,
                                                     amount_0="250", factor_0="125")).text
    assert not g.objects("quantity_unit_conversions", product_id=sugar)
    assert 'name="amount_0" value=""' in html and 'name="factor_0" value=""' in html and "1 cup =" in html
    egg_guess = ProductGuess(generic_name="x", is_new_generic=True, stock_unit="g", package_amount=None,
                             location="Pantry", best_before_days=30, g_per_unit=1)
    ai.guess = egg_guess  # same for a product created from the row
    c.post(f"/import/{pid}", data=review_form(action="create:0", new_name_0="Bread flour", product_0="",
                                              prev_0=flour, factor_0="125"))
    bread = next(p["id"] for p in g.objects("products") if p["name"] == "Bread flour")
    assert not g.objects("quantity_unit_conversions", product_id=bread)


def test_review_save_runs_off_the_event_loop_and_only_once(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowGrocy(FakeGrocy):
        def create(self, entity, data):
            if entity == "recipes":
                started.set()
                release.wait(5)
            return super().create(entity, data)

    g = SlowGrocy()
    bootstrap(g)
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    store = PendingStore(tmp_path / "pending")
    app = create_app(g, FakeAI(picks=Picks(items=[]), draft=soup_draft()),
                     httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), store)
    form = review_form(product_0=flour, prev_0=flour, factor_0="125", product_1=egg, prev_1=egg, amount_1="1")
    codes = {}
    with TestClient(app) as c:  # one event loop shared by all requests, like uvicorn
        c.post("/import", data={"urls": "https://example.com/soup"})
        pid = store.all()[0].id

        def save(k):
            codes[k] = c.post(f"/import/{pid}", data=form, follow_redirects=False).status_code

        first = threading.Thread(target=save, args=("first",))
        first.start()
        assert started.wait(5)
        t0 = time.monotonic()
        assert c.get("/pantry").status_code == 200 and time.monotonic() - t0 < 2  # loop not blocked by the save
        second = threading.Thread(target=save, args=("second",))  # double-tapped Save
        second.start()
        time.sleep(0.3)
        release.set()
        first.join(5)
        second.join(5)
    assert codes == {"first": 303, "second": 303}
    assert len(g.objects("recipes")) == 1 and store.get(pid) is None


# ---- plan and shopping ------------------------------------------------------------------------------------------
def _monday(d):
    return d - timedelta(days=d.weekday())


def _future_day(weekday=None):
    """A day within the shopping window (tomorrow or later); weekday 0-3 gives a 4-serving dinner."""
    d = date.today() + timedelta(days=1)
    while weekday is not None and d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def flour_recipe(g, per=150, stock=None):
    i = ids(g)
    pid = product(g, "Flour")
    rid = g.create("recipes", {"name": "Bread", "base_servings": 2})
    g.create("recipes_pos", {"recipe_id": rid, "product_id": pid, "amount": per, "qu_id": i.unit["g"]})
    if stock:
        g.add_stock(pid, stock, None, i.location["Pantry"])
    return pid, rid


def test_plan_post_adds_leftover_and_list_item(env):
    g, _, _, c, _ = env
    pid, rid = flour_recipe(g)
    day = _future_day(1)  # Tuesday dinner: 4 servings + leftovers next day
    r = c.post("/plan", data={"day": day.isoformat(), "section": "Dinner", "recipe_id": rid, "servings": ""},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/plan?week={_monday(day).isoformat()}"
    assert [n["note"] for n in g.objects("meal_plan", type="note")] == ["Leftovers: Bread"]
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])
    assert row["product_id"] == pid and row["amount"] == 300
    html = c.get(f"/plan?week={_monday(day).isoformat()}").text
    assert "Leftovers: Bread" in html and "Cooked it" in html and "Suggest week" in html


def test_plan_rejects_bad_servings(env):
    g, _, _, c, _ = env
    _, rid = flour_recipe(g)
    for bad in ("0", "-2", "abc", "2.5", "inf", "nan"):
        r = c.post("/plan", data={"day": "2026-11-20", "section": "Dinner", "recipe_id": rid, "servings": bad})
        assert r.status_code == 400, bad
    assert g.objects("meal_plan") == []


def test_plan_remove_drops_entry_and_leftover(env):
    g, _, _, c, _ = env
    _, rid = flour_recipe(g)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})
    [e] = g.objects("meal_plan", type="recipe")
    c.post(f"/plan/{e['id']}/remove")
    assert g.objects("meal_plan") == []
    assert c.post("/plan/999/remove").status_code == 404


def test_cook_route_flashes_shortage(env):
    g, _, _, c, _ = env
    _, rid = flour_recipe(g, stock=100)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})
    [e] = g.objects("meal_plan", type="recipe")
    r = c.post(f"/plan/{e['id']}/cook")  # follows the redirect back to /plan
    assert "You were short on: Flour" in r.text and 'href="/pantry"' in r.text
    assert g.objects("meal_plan", type="recipe")[0]["done"] == 1
    pid2, rid2 = flour_recipe(g, stock=1000)
    c.post("/plan", data={"day": _future_day(2).isoformat(), "section": "Dinner", "recipe_id": rid2})
    e2 = [x for x in g.objects("meal_plan", type="recipe") if x["recipe_id"] == rid2][0]
    assert "You were short on" not in c.post(f"/plan/{e2['id']}/cook").text


def test_suggest_lists_picks_with_accept(env):
    from app.ai import Suggestion, WeekSuggestions
    g, ai, _, c, _ = env
    _, rid = flour_recipe(g)
    monday = _monday(date.today() + timedelta(days=7))
    ai.week = WeekSuggestions(items=[Suggestion(day=monday.isoformat(), section="Dinner", recipe_id=rid,
                                                reason="uses <b>flour</b>")])
    r = c.post("/plan/suggest", data={"week": monday.isoformat()})
    assert "Accept" in r.text and "uses &lt;b&gt;flour&lt;/b&gt;" in r.text and "<b>flour</b>" not in r.text
    assert c.post("/plan/suggest", data={"week": (monday + timedelta(days=1)).isoformat()}).status_code == 200


def test_shopping_shows_walmart_link_with_store(env, tmp_path):
    g, ai, store, _, _ = env
    pid, rid = flour_recipe(g)
    g.set_userfields("products", pid, {"walmart_item_id": "10535106", "package_amount": 2268})
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", "5435", 6, tmp_path, 3), store)
    c = TestClient(app)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid, "servings": "4"})
    html = c.get("/shopping").text
    assert "addToCart?items=10535106&amp;storeId=5435" in html
    assert "Flour" in html and "1 × 2268 g pack" in html
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])
    c.post(f"/shopping/{row['id']}/toggle")  # ticked = already got it, so out of the hand-off
    assert "addToCart" not in c.get("/shopping").text


def test_shopping_lists_unmapped_products_with_search_link(env):
    g, _, _, c, _ = env
    _, rid = flour_recipe(g)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})
    html = c.get("/shopping").text
    assert "Still to add on Walmart" in html and "walmart.com/search?q=Flour" in html
    assert "Open Walmart cart" not in html


def test_bought_moves_to_stock_and_rebuilds(env):
    g, _, _, c, _ = env
    pid, rid = flour_recipe(g)
    g.set_userfields("products", pid, {"package_amount": 1000})
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})  # needs 300 g
    extras = ids(g).list["Extras"]
    c.post("/shopping/extra", data={"name": "birthday candles"})
    c.post("/shopping/extra", data={"name": "  "})  # blank ignored
    for lid in ids(g).list.values():  # each redirect rebuilds the Meal plan list, so ids are re-read per list
        for r in g.objects("shopping_list", shopping_list_id=lid):
            c.post(f"/shopping/{r['id']}/toggle")
    untouched = g.create("shopping_list", {"shopping_list_id": extras, "note": "later", "amount": 1, "done": 0})
    assert c.post("/shopping/bought", follow_redirects=False).status_code == 303
    [entry] = g.stock[pid]
    assert entry["amount"] == 1000 and entry["location_id"] == ids(g).location["Pantry"]
    assert entry["best_before_date"] == (date.today() + timedelta(days=5)).isoformat()
    assert [x["id"] for x in g.objects("shopping_list", shopping_list_id=extras)] == [untouched]
    assert g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"]) == []  # 1000 g covers 300 g


def test_walmart_paste_sets_userfield(env):
    g, _, _, c, _ = env
    pid = product(g, "Milk")
    r = c.post(f"/products/{pid}/walmart", data={"link": "https://www.walmart.com/ip/Milk/10450114"})
    assert r.status_code == 200 and g.userfields("products", pid)["walmart_item_id"] == "10450114"
    r = c.post(f"/products/{pid}/walmart", data={"link": "https://evil.example/ip/1/2"})
    assert "does not look like" in r.text and g.userfields("products", pid)["walmart_item_id"] == "10450114"
    assert c.post("/products/999/walmart", data={"link": "123"}).status_code == 404


def test_api_expiring_message(env):
    g, _, _, c, _ = env
    assert c.get("/api/expiring").json() == {"count": 0, "message": ""}
    milk, spinach = product(g, "milk"), product(g, "spinach")
    g.volatile_result["due_products"] = [{"product_id": milk, "best_before_date": "2026-11-20"},
                                         {"product_id": spinach, "best_before_date": "2026-11-21 00:00:00"}]
    assert c.get("/api/expiring").json() == {"count": 2, "message": "Use soon: milk (Fri), spinach (Sat)"}
    yogurt = product(g, "yogurt")
    g.volatile_result["expired_products"] = [{"product_id": yogurt, "best_before_date": "2026-11-10"}]
    assert c.get("/api/expiring").json() == {
        "count": 3, "message": "Expired: yogurt; Use soon: milk (Fri), spinach (Sat)"}


def test_plan_and_shopping_pages_render_empty(env):
    c = env[3]
    for url in ("/plan", "/shopping", "/plan?week=2026-11-18"):
        assert c.get(url).status_code == 200, url


def test_toggle_from_stale_page_still_works(env):
    g, _, _, c, _ = env
    _, rid = flour_recipe(g)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])
    c.get("/shopping")  # another page load rebuilds the list
    assert c.post(f"/shopping/{row['id']}/toggle").status_code == 200
    assert g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])[0]["done"] == 1


def test_double_tapped_bought_stocks_once(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowGrocy(FakeGrocy):
        def add_stock(self, *a):
            started.set()
            release.wait(5)
            super().add_stock(*a)

    g = SlowGrocy()
    bootstrap(g)
    pid = product(g, "Flour")
    g.create("shopping_list", {"shopping_list_id": ids(g).list["Extras"], "product_id": pid, "amount": 5, "done": 1})
    app = create_app(g, FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"))
    with TestClient(app) as c:
        def tap():
            c.post("/shopping/bought", follow_redirects=False)

        first = threading.Thread(target=tap)
        first.start()
        assert started.wait(5)
        second = threading.Thread(target=tap)
        second.start()
        time.sleep(0.3)
        release.set()
        first.join(5)
        second.join(5)
    assert sum(e["amount"] for e in g.stock[pid]) == 5


def test_remaining_suggestions_survive_accepting_one(env):
    from app.ai import Suggestion, WeekSuggestions
    g, ai, _, c, _ = env
    _, rid = flour_recipe(g)
    monday = _monday(date.today() + timedelta(days=7))
    d1, d2 = monday, monday + timedelta(days=1)
    ai.week = WeekSuggestions(items=[Suggestion(day=d.isoformat(), section="Dinner", recipe_id=rid, reason=f"why {i}")
                                     for i, d in enumerate((d1, d2))])
    c.post("/plan/suggest", data={"week": monday.isoformat()})
    html = c.post("/plan", data={"day": d1.isoformat(), "section": "Dinner", "recipe_id": rid}).text  # accept first
    assert "why 1" in html and "why 0" not in html
    assert "why 1" in c.get(f"/plan?week={monday.isoformat()}").text  # still there on a later visit
    c.post("/plan/suggest/clear", data={"week": monday.isoformat()})
    assert "why 1" not in c.get(f"/plan?week={monday.isoformat()}").text


def test_cross_site_posts_are_refused(env):
    g, _, _, c, _ = env
    r = c.post("/shopping/extra", data={"name": "Milk"}, headers={"Sec-Fetch-Site": "cross-site"},
               follow_redirects=False)
    assert r.status_code == 403 and not g.objects("shopping_list")
    assert c.get("/pantry", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    for h in ({}, {"Sec-Fetch-Site": "same-origin"}):  # curl/HA send no header; the app's own pages same-origin
        assert c.post("/shopping/extra", data={"name": "Milk"}, headers=h, follow_redirects=False).status_code == 303
    assert len(g.objects("shopping_list")) == 2


def test_relink_refused_when_stock_is_in_another_unit(env):
    g, _, _, c, _ = env
    child = product(g, "Brand Pasta", parent_product_id=product(g, "Pasta"))
    product(g, "Noodles", "piece")
    g.add_stock(child, 500, None, None)
    r = c.post("/api/relink", json={"product_id": child, "generic_name": "Noodles"})
    assert r.status_code == 400 and "in stock in another unit" in r.json()["message"]


def test_scan_with_unknown_package_size_asks_for_amount(env):
    g, _, _, c, _ = env
    pid = product(g, "Brand Rice", parent_product_id=product(g, "Rice"))
    g.create("product_barcodes", {"product_id": pid, "barcode": "321"})
    for action in ("add", "use"):
        r = c.post("/api/scan", json={"code": "321", "action": action}).json()
        assert r["status"] == "need_amount" and r["unit"] == "g" and r["product"] == "Brand Rice"
        assert r["action"] == action
    assert pid not in g.stock
    r = c.post("/api/scan", json={"code": "321", "action": "add", "amount": 300}).json()
    assert r["status"] == "ok" and r["amount"] == 300 and g.stock_amounts()[pid] == 300
    assert c.post("/api/scan", json={"code": "321", "action": "use", "amount": 120}).json()["amount"] == 120
    assert g.stock_amounts()[pid] == 180
    for bad in (0, -5, "inf", "nan"):
        assert c.post("/api/scan", json={"code": "321", "action": "add", "amount": bad}).status_code == 422


def _barcoded(g, name="Brand Rice", code="321", **kw):
    pid = product(g, name, parent_product_id=product(g, name.split()[-1]), **kw)
    g.create("product_barcodes", {"product_id": pid, "barcode": code})
    return pid


def test_add_with_typed_amount_remembers_package_size(env):
    g, _, _, c, _ = env
    pid = _barcoded(g)
    assert c.post("/api/scan", json={"code": "321", "action": "add"}).json()["status"] == "need_amount"
    r = c.post("/api/scan", json={"code": "321", "action": "add", "amount": 32}).json()
    assert r["status"] == "ok" and r["amount"] == 32
    assert g.userfields("products", pid)["package_amount"] == 32
    r = c.post("/api/scan", json={"code": "321", "action": "add"}).json()
    assert r["status"] == "ok" and r["amount"] == 32 and g.stock_amounts()[pid] == 64


def test_typed_amount_never_overwrites_known_size(env):
    g, _, _, c, _ = env
    pid = _barcoded(g)
    g.set_userfields("products", pid, {"package_amount": 32})
    c.post("/api/scan", json={"code": "321", "action": "add", "amount": 5})
    assert g.userfields("products", pid)["package_amount"] == 32


def test_use_with_amount_is_not_remembered(env):
    g, _, _, c, _ = env
    pid = _barcoded(g)
    g.add_stock(pid, 500, None, None)
    c.post("/api/scan", json={"code": "321", "action": "use", "amount": 120})
    assert "package_amount" not in g.userfields("products", pid)


def test_supply_used_one_takes_one_unit(env):
    g, _, _, c, _ = env
    bags = supply(g, stock=64, package_amount=32)
    c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 63


def test_scan_use_on_supply_takes_one_unit(env):
    g, _, _, c, _ = env
    bags = supply(g, stock=64, package_amount=32)
    g.create("product_barcodes", {"product_id": bags, "barcode": "555"})
    r = c.post("/api/scan", json={"code": "555", "action": "use"}).json()
    assert r["status"] == "ok" and r["amount"] == 1 and g.stock_amounts()[bags] == 63
    bare = supply(g, "Sponges", stock=5)  # no package size: still never asks
    g.create("product_barcodes", {"product_id": bare, "barcode": "556"})
    r = c.post("/api/scan", json={"code": "556", "action": "use"}).json()
    assert r["status"] == "ok" and g.stock_amounts()[bare] == 4


def test_scan_use_on_food_still_takes_a_package(env):
    g, _, _, c, _ = env
    pid = _barcoded(g, "Brand Beans", "777")
    g.set_userfields("products", pid, {"package_amount": 400})
    g.add_stock(pid, 1200, None, None)
    assert c.post("/api/scan", json={"code": "777", "action": "use"}).json()["amount"] == 400
    assert g.stock_amounts()[pid] == 800


def test_bought_without_package_size_adds_row_amount(env):
    g, _, _, c, _ = env
    pid, rid = flour_recipe(g)  # no package_amount
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})  # needs 300 g
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])
    c.post(f"/shopping/{row['id']}/toggle")
    c.post("/shopping/bought")
    assert g.stock_amounts()[pid] == 300


def test_pantry_machine_read_amounts_are_not_rounded_up(env):
    g, _, _, c, _ = env
    pid = product(g, "Rice")
    g.add_stock(pid, 2189.1366, None, ids(g).location["Pantry"])
    html = c.get("/pantry").text
    assert 'name="amount" value="2189.1366"' in html and 'max="2189.1366"' in html


def test_plan_add_and_remove_wait_for_the_plan_lock(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowGrocy(FakeGrocy):
        def add_stock(self, *a):
            started.set()
            release.wait(5)
            super().add_stock(*a)

    g = SlowGrocy()
    bootstrap(g)
    pid, rid = flour_recipe(g)
    gone = g.create("meal_plan", {"day": _future_day(4).isoformat(), "type": "recipe", "recipe_id": rid,
                                  "recipe_servings": 2, "section_id": ids(g).section["Dinner"], "note": "", "done": 0})
    g.create("shopping_list", {"shopping_list_id": ids(g).list["Extras"], "product_id": pid, "amount": 5, "done": 1})
    app = create_app(g, FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"))
    with TestClient(app) as c:
        bought = threading.Thread(target=lambda: c.post("/shopping/bought"))
        bought.start()
        assert started.wait(5)  # Bought holds the plan lock
        waiting = [threading.Thread(target=lambda: c.post("/plan", data={
                       "day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})),
                   threading.Thread(target=lambda: c.post(f"/recipes/{rid}/plan", data={
                       "day": _future_day(2).isoformat(), "section": "Dinner"})),
                   threading.Thread(target=lambda: c.post(f"/plan/{gone}/remove"))]
        for t in waiting:
            t.start()
        time.sleep(0.3)
        during = [r["id"] for r in g.objects("meal_plan")]
        release.set()
        for t in [bought, *waiting]:
            t.join(5)
    assert during == [gone]  # neither ran while Bought held the lock
    assert len(g.objects("meal_plan", type="recipe")) == 2 and g.get_object("meal_plan", gone) is None


def test_saved_table_has_real_amount_for_packet_rows(env):
    g, ai, store, c, _ = env
    sour = product(g, "Sour cream")
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="packet", name="sour cream", note="est.")],
                 ops=[Op(id="o1", label="mix", inputs=["i0"])])
    ai.draft = RecipeDraft(title="Tacos", servings=2, steps=["Stir"], table=t)
    c.post("/import", data={"urls": "https://example.com/tacos"})
    pid = store.all()[0].id
    r = c.post(f"/import/{pid}", data=dict(product_0=sour, prev_0=sour, amount_0="57", new_name_0="sour cream",
                                           op_id=["o1"], op_label=["mix"], op_in_0=["i0"], action="save"),
               follow_redirects=False)
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    [ing] = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients
    assert (ing.qty, ing.unit, ing.name, ing.note) == (57, "g", "sour cream", "est.")
    assert "1 packet" not in c.get(f"/recipes/{rid}").text


# ---- people and device identity -------------------------------------------------------------------------------------
from app.house import HouseStore  # noqa: E402
from tests.fakes import FakeHA  # noqa: E402


@pytest.fixture
def hv(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    ha = FakeHA()
    ha.persons_map = {"person.jhamlin": "Jhamlin", "person.catherine": "Catherine"}
    ha.trackers = {"person.jhamlin": ["device_tracker.pixel_10_pro"], "person.catherine": ["device_tracker.cat_s_pixel_10"]}
    ha.services = ["mobile_app_pixel_10_pro", "mobile_app_cat_s_pixel_10"]
    house = HouseStore(tmp_path / "house.db")
    app = create_app(g, FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"),
                     house=house, ha=ha)
    return g, house, ha, TestClient(app)


def test_people_form_creates_grocy_user(hv):
    g, house, ha, c = hv
    r = c.post("/home/people", data={"name": "Josh", "ha_person": "person.jhamlin",
                                     "notify_service": "mobile_app_pixel_10_pro"}, follow_redirects=False)
    assert r.status_code == 303
    assert [u["first_name"] for u in g.users()] == ["Josh"]
    (p,) = house.people()
    assert (p.name, p.ha_person, p.notify_service, p.grocy_user_id) == (
        "Josh", "person.jhamlin", "mobile_app_pixel_10_pro", g.users()[0]["id"])


def test_people_form_fills_phone_from_chosen_person(hv):
    _, house, _, c = hv
    c.post("/home/people", data={"name": "Cat", "ha_person": "person.catherine", "notify_service": ""})
    assert house.people()[0].notify_service == "mobile_app_cat_s_pixel_10"
    c.post("/home/people", data={"name": "Jham", "ha_person": "", "notify_service": ""})  # person matched by name
    assert house.people()[1].ha_person == "person.jhamlin"
    assert house.people()[1].notify_service == "mobile_app_pixel_10_pro"


def test_name_match_ignores_person_prefix_and_fragments(hv):
    _, house, _, c = hv
    c.post("/home/people", data={"name": "on", "ha_person": "", "notify_service": ""})  # "person." and "son" must not match
    assert house.people()[0].ha_person == ""


def test_explicit_no_phone_sticks_blank_falls_back(hv):
    _, house, _, c = hv
    c.post("/home/people", data={"name": "Cat", "ha_person": "person.catherine", "notify_service": "none"})
    c.post("/home/people", data={"name": "Cathy", "ha_person": "person.catherine", "notify_service": ""})
    assert [p.notify_service for p in house.people()] == ["", "mobile_app_cat_s_pixel_10"]


def test_duplicate_name_refused(hv):
    _, house, _, c = hv
    c.post("/home/people", data={"name": "Josh"})
    r = c.post("/home/people", data={"name": "josh"})
    assert r.status_code == 400 and "josh is already on the list." in r.text and len(house.people()) == 1


def test_bad_notify_service_refused(hv):
    _, house, _, c = hv
    for bad in ("persistent_notification", "mobile_app_../../x", "mobile_app_"):
        r = c.post("/home/people", data={"name": "Josh", "notify_service": bad})
        assert r.status_code == 400 and "Pick a phone from the list." in r.text and not house.people()


def test_people_row_wraps_long_phone(hv):
    _, house, _, c = hv
    house.save_person("Cat", 1, "", "mobile_app_cat_s_pixel_10")
    html = c.get("/home/people").text
    assert "breakable" in html and "style=" not in html and 'class="field"' in html
    assert "Automatic uses the phone Home Assistant tracks" in html


def test_who_cookie_set_and_used(hv):
    _, house, _, c = hv
    pid = house.save_person("Josh", 1, "person.jhamlin", "mobile_app_pixel_10_pro")
    assert "Josh" in c.get("/who").text and "Kitchen (everyone)" in c.get("/who").text
    r = c.post("/who", data={"who": str(pid)}, follow_redirects=False)
    assert r.status_code == 303 and "who=" in r.headers["set-cookie"]
    assert "This device is Josh" in c.get("/home/people").text
    c.post("/who", data={"who": "kitchen"})
    assert "This device is the kitchen" in c.get("/home/people").text


def test_people_form_lists_ha_choices(hv):
    html = hv[3].get("/home/people").text
    for v in ("person.jhamlin", "person.catherine", "mobile_app_pixel_10_pro", "mobile_app_cat_s_pixel_10"):
        assert f'value="{v}"' in html


def test_people_page_without_ha_says_so(env):
    html = env[3].get("/home/people").text
    assert "Home Assistant isn't set up" in html


def test_delete_person(hv):
    _, house, _, c = hv
    pid = house.save_person("Josh", 1, "", "")
    c.post(f"/home/people/{pid}/delete")
    assert house.people() == []


def test_who_and_people_live_in_me(hv):
    c = hv[3]
    assert 'href="/me" aria-current="page"' in c.get("/who").text
    people = c.get("/home/people").text
    assert 'href="/me" aria-current="page"' in people and 'href="/home" aria-current' not in people


# ---- household supplies -----------------------------------------------------------------------------------------
def supply(g, name="Trash bags", min_stock=2, stock=0, walmart_item_id=None, package_amount=None, **fields):
    group = next(x["id"] for x in g.objects("product_groups") if x["name"] == "Household supplies")
    pid = product(g, name, "piece", product_group_id=group, min_stock_amount=min_stock, default_best_before_days=-1,
                  location_id=ids(g).location["Garage"], **fields)
    if stock:
        g.add_stock(pid, stock, None, ids(g).location["Garage"])
    uf = {k: v for k, v in (("walmart_item_id", walmart_item_id), ("package_amount", package_amount)) if v}
    if uf:
        g.set_userfields("products", pid, uf)
    return pid


def test_today_get_does_not_write_running_low(env):
    g, _, _, c, _ = env
    supply(g, stock=1)
    c.cookies.set("who", "kitchen")
    html = c.get("/").text
    assert "Trash bags" in html and "Running low" in html
    assert g.objects("shopping_list", shopping_list_id=ids(g).list["Running low"]) == []


def test_new_supply_asks_keep_at_least(env):
    g, ai, _, c, _ = env
    ai.guess = ProductGuess(generic_name="Trash bags", is_new_generic=True, stock_unit="piece", package_amount=40,
                            location="Garage", best_before_days=-1, g_per_unit=20, category="supply")
    r = c.post("/api/scan/photo", data={"code": "4242", "action": "add"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "need_min" and r["unit"] == "piece" and r["default"] == 40 and r["action"] == "add"
    pid = r["product_id"]
    assert pid not in g.stock
    for bad in (0, -1, "inf"):
        assert c.post(f"/api/products/{pid}/min", json={"amount": bad}).status_code == 422
    assert c.post("/api/products/999/min", json={"amount": 2}).status_code == 404
    assert c.post(f"/api/products/{pid}/min", json={"amount": 2}).json() == {"status": "ok"}
    generic = g.get_object("products", g.get_object("products", pid)["parent_product_id"])
    assert generic["min_stock_amount"] == 2 and generic["cumulate_min_stock_amount_of_sub_products"] == 1
    assert "min_stock_amount" not in g.get_object("products", pid)  # kept on the generic, not the brand
    r = c.post("/api/scan", json={"code": "4242", "action": "add"}).json()  # the add continues
    assert r["status"] == "ok" and r["amount"] == 40 and g.stock_amounts()[pid] == 40
    assert g.stock[pid][0]["best_before_date"] is None  # never expires
    assert c.post("/api/scan", json={"code": "4242", "action": "add"}).json()["status"] == "ok"  # asked once only
    ai.guess = ai.guess.model_copy(update={"is_new_generic": False})  # a second brand of the same generic
    r = c.post("/api/scan/photo", data={"code": "4343", "action": "add"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "ok" and r["product_id"] != pid  # once per generic, not per brand


def test_running_low_in_walmart_cart(env):
    g, _, _, c, _ = env
    bags = supply(g, stock=1, walmart_item_id="777", package_amount=40)
    soap = supply(g, "Dish soap", min_stock=1)
    extras = ids(g).list["Extras"]
    c.post("/shopping/extra", data={"name": "candles"})
    html = c.get("/shopping").text
    assert html.index("<h2>Meal plan</h2>") < html.index("<h2>Running low</h2>") < html.index("<h2>Extras</h2>")
    assert "addToCart?items=777" in html and "walmart.com/search?q=Dish+soap" in html
    low = ids(g).list["Running low"]
    assert sorted((r["product_id"], r["amount"]) for r in g.objects("shopping_list", shopping_list_id=low)) == \
        [(bags, 1), (soap, 1)]
    row = next(r for r in g.objects("shopping_list", shopping_list_id=low) if r["product_id"] == bags)
    c.post(f"/shopping/{row['id']}/toggle")
    assert "addToCart" not in c.get("/shopping").text  # ticked = already got it
    c.post("/shopping/bought")
    assert g.stock_amounts()[bags] == 41 and g.stock[bags][-1]["best_before_date"] is None
    assert [r["product_id"] for r in g.objects("shopping_list", shopping_list_id=low)] == [soap]
    assert [r["note"] for r in g.objects("shopping_list", shopping_list_id=extras)] == ["candles"]


def test_supplies_view_used_one(env):
    g, _, _, c, _ = env
    bags = supply(g, min_stock=2, cumulate_min_stock_amount_of_sub_products=1)  # a generic with two brands
    glad = supply(g, "Glad bags", min_stock=0, stock=3, parent_product_id=bags)
    hefty = supply(g, "Hefty bags", min_stock=0, stock=2, parent_product_id=bags)
    sponge = supply(g, "Sponges", min_stock=0, stock=3)
    html = c.get("/home/supplies").text
    assert "Trash bags" in html and "Sponges" in html and "Glad bags" not in html and "Hefty bags" not in html
    assert re.search(r'Trash bags</b></p>\s*<p class="muted">5 left\.</p>', html)  # the total across brands
    assert '<p class="muted">3 left.</p>' in html and "pieces</span>" in html
    assert f'action="/home/supplies/{sponge}/used"' in html and "Used one" in html and "Keep at least" in html
    assert f'/home/supplies/{glad}/' not in html
    r = c.post(f"/home/supplies/{bags}/used", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/home/supplies")
    assert g.stock_amounts()[bags] == 4  # no package size: one unit, taken from a brand
    g.set_userfields("products", bags, {"package_amount": 10})
    c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 3  # a known package size no longer matters: still one unit
    for _ in range(3):
        c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 0
    assert c.post(f"/home/supplies/{bags}/used").status_code == 200  # nothing left: no error
    assert '<p class="warn">None left. Running low.</p>' in c.get("/home/supplies").text
    low = ids(g).list["Running low"]
    assert [(r["product_id"], r["amount"]) for r in g.objects("shopping_list", shopping_list_id=low)] == [(bags, 2)]
    assert hefty not in [r["product_id"] for r in g.objects("shopping_list", shopping_list_id=low)]
    c.post(f"/home/supplies/{sponge}/min", data={"amount": 4})
    assert g.get_object("products", sponge)["min_stock_amount"] == 4
    assert sorted(r["product_id"] for r in g.objects("shopping_list", shopping_list_id=low)) == [bags, sponge]
    c.post(f"/home/supplies/{glad}/min", data={"amount": 3})  # a brand's id still sets its generic
    assert g.get_object("products", bags)["min_stock_amount"] == 3 and g.get_object("products", glad)["min_stock_amount"] == 0
    assert c.post(f"/home/supplies/{sponge}/min", data={"amount": -1}).status_code == 422
    assert c.post("/home/supplies/999/used").status_code == 404


def test_kitchen_pages_use_shell(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})
    for url in ("/plan", "/shopping", "/recipes", f"/recipes/{rid}"):
        html = c.get(url).text
        assert 'data-section="kitchen"' in html and "style=" not in html, url


def test_shopping_has_three_sections(env):
    html = env[3].get("/shopping").text
    heads = [h for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html)]
    assert [h for h in heads if h in ("Meal plan", "Running low", "Extras")] == ["Meal plan", "Running low", "Extras"]


def test_remaining_pages_use_shell(env, tmp_path):
    g, ai, store, c, _ = env
    rid = brownie_recipe(g)
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    g.add_stock(product(g, "Yogurt"), 500, date.today() + timedelta(days=2), ids(g).location["Fridge"])
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    down = TestClient(create_app(DownGrocy(httpx.ConnectError("refused")), FakeAI(),
                                 httpx.Client(transport=httpx.MockTransport(no_network)), settings,
                                 PendingStore(tmp_path / "down"))).get("/recipes")
    pages = {url: c.get(url).text for url in ("/pantry", "/scan", "/import", f"/import/{pid}", f"/recipes/{rid}/table")}
    pages["down"] = down.text
    for url, html in pages.items():
        assert 'data-section="kitchen"' in html and "style=" not in html, url
        assert "·" not in html and "→" not in html, url  # no middle dots or arrows in copy


def test_scan_prompts_are_in_page(env):
    html = env[3].get("/scan").text
    sheet = html[html.index('<dialog id="ask"'):]
    sheet = sheet[:sheet.index("</dialog>")]
    assert 'type="number"' in sheet and 'class="card-actions"' in sheet and sheet.count("<button") == 2
    assert 'id="scan-result"' in html and html.index('id="scanner"') < html.index('id="scan-result"')
    js = env[3].get("/static/app.js").text
    assert "prompt(" not in js and "we'll remember it." in js and "Keep at least how many?" in js


def test_pantry_keeps_supplies_out_and_reads_like_supplies(env):
    g, _, _, c, _ = env
    i = ids(g)
    eggs = product(g, "Eggs", "piece")
    g.add_stock(eggs, 12, date.today() + timedelta(days=2), i.location["Fridge"])
    rice = product(g, "Rice")
    g.add_stock(rice, 500, None, i.location["Pantry"])
    supply(g, "Toilet paper", stock=6)
    g.volatile_result["due_products"] = [{"product_id": eggs, "amount": 12,
                                          "best_before_date": (date.today() + timedelta(days=2)).isoformat()}]
    html = c.get("/pantry").text
    assert "Toilet paper" not in html and "<h2>Garage</h2>" not in html
    assert "12 left" in html and "500 g left" in html and "12 piece" not in html
    day = f"{date.today() + timedelta(days=2):%A}"
    assert f"Use by {day}" in html and f"Best before {day}" in html


def test_scan_dialogs_save_on_enter_and_new_scan_clears_result(env):
    html = env[3].get("/scan").text
    for dialog in ('<dialog id="ask"', '<dialog id="fix"'):
        d = html[html.index(dialog):]
        d = d[:d.index("</dialog>")]
        assert d.index('value="ok"') < d.index('value="cancel"'), dialog  # Enter submits the first button
    js = env[3].get("/static/app.js").text
    param = re.search(r"decodeFromVideoDevice\(undefined, video, \((\w+)\) =>", js).group(1)
    hidden = re.search(r"(\w+)\.hidden = true;\s*run\(`Looking up", js).group(1)
    assert hidden != param and f'const {hidden} = document.getElementById("scan-result")' in js


def test_typed_pack_size_reaches_running_low_bought(env):
    """Running low names the generic, so a size typed for a new brand is kept on its generic too."""
    g, ai, _, c, _ = env
    bags = supply(g)  # keep at least 2, no package size
    ai.guess = ProductGuess(generic_name="Trash bags", is_new_generic=False, stock_unit="piece", package_amount=None,
                            location="Garage", best_before_days=-1, g_per_unit=20, category="supply")
    r = c.post("/api/scan/photo", data={"code": "5151", "action": "add"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "need_amount"
    assert c.post("/api/scan", json={"code": "5151", "action": "add", "amount": 32}).json()["status"] == "ok"
    assert g.userfields("products", bags)["package_amount"] == 32
    g.set_amount(r["product_id"], 1)  # used down to one: running low by 1
    c.get("/shopping")
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Running low"])
    c.post(f"/shopping/{row['id']}/toggle")
    c.post("/shopping/bought")
    assert g.stock_amounts()[bags] == 33  # one package of 32, not the shortfall of 1


def test_tap_targets_are_at_least_48px():
    css = (Path(__file__).parent.parent / "app/static/style.css").read_text()
    for sel in (".segmented > *", ".rail-sub a", ".stock-more summary"):
        rule = css[css.index(sel + " {"):]
        assert "min-height: 48px" in rule[:rule.index("}")], sel


# ---- dish photos and the table-first recipe page -----------------------------------------------------------------
def _png(w=300, h=200):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "PNG")
    return buf.getvalue()


def test_photo_upload_validates_and_serves(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    assert c.get(f"/recipes/{rid}/photo").status_code == 404  # no picture yet
    assert "Add a photo" in c.get(f"/recipes/{rid}").text
    alert = lambda html: re.findall(r'<p class="card error" role="alert">([^<]*)</p>', html)  # noqa: E731
    r = c.post(f"/recipes/{rid}/photo", files={"photo": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and alert(r.text) == ["That file isn&#39;t a photo."] and 'class="cfe-wrap"' in r.text
    r = c.post(f"/recipes/{rid}/photo", files={"photo": ("a.png", b"not really", "image/png")})
    assert r.status_code == 400 and alert(r.text) == ["That file isn&#39;t a photo."]  # says image, isn't one
    r = c.post(f"/recipes/{rid}/photo", files={"photo": ("a.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")})
    assert r.status_code == 413 and alert(r.text) == ["That photo is bigger than 10 MB."]
    assert alert(c.get(f"/recipes/{rid}").text) == []
    assert g.get_object("recipes", rid).get("picture_file_name") is None
    r = c.post(f"/recipes/{rid}/photo", files={"photo": ("a.png", _png(), "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/recipes/{rid}"
    name = g.get_object("recipes", rid)["picture_file_name"]
    r = c.get(f"/recipes/{rid}/photo?w=5000")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8" and "max-age=86400" in r.headers["cache-control"]
    assert r.headers["content-type"] == "image/jpeg"
    html = c.get(f"/recipes/{rid}").text
    assert "Change photo" in html and name in c.get("/recipes").text  # the name busts caches when it changes
    assert c.get("/recipes/999/photo").status_code == 404
    c.post(f"/recipes/{rid}/photo", files={"photo": ("b.png", _png(400, 300), "image/png")})  # replace it
    new = g.get_object("recipes", rid)["picture_file_name"]
    assert new != name and list(g.pictures) == [new]  # the old file is gone from Grocy


def test_photo_route_passes_grocy_through(env):
    g, _, _, c, _ = env
    rid = g.create("recipes", {"name": "Chili", "base_servings": 2, "picture_file_name": "chili.webp"})
    g.recipe_picture = lambda name, w: (b"RIFF", "image/webp")
    r = c.get(f"/recipes/{rid}/photo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"

    def broken(name, w):
        raise GrocyError("GET recipe picture -> 500")
    g.recipe_picture = broken
    assert c.get(f"/recipes/{rid}/photo").status_code == 404


def test_recipes_list_shows_photo_cards(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    g.create("recipes", {"name": "Chili", "base_servings": 2})
    c.post(f"/recipes/{rid}/photo", files={"photo": ("a.png", _png(), "image/png")})
    html = c.get("/recipes").text
    assert re.search(rf'<img[^>]*src="/recipes/{rid}/photo\?w=\d+[^"]*"[^>]*alt="Brownies"[^>]*loading="lazy"', html) \
        or re.search(rf'<img[^>]*src="/recipes/{rid}/photo\?w=\d+[^"]*"[^>]*loading="lazy"[^>]*alt="Brownies"', html)
    chili = next(li for li in html.split("<li>") if "Chili" in li)
    assert 'class="photo-none"' in chili and "<img" not in chili


def test_review_shows_found_photo(env):
    g, ai, store, c, _ = env
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"})
    p = store.all()[0]
    assert "No photo found" in c.get(f"/import/{p.id}").text
    assert c.get(f"/import/{p.id}/photo").status_code == 404
    store.save_photo(p.id, _png())
    html = c.get(f"/import/{p.id}").text
    assert f'src="/import/{p.id}/photo"' in html and "No photo found" not in html
    assert c.get(f"/import/{p.id}/photo").content == _png()


def test_recipe_page_leads_with_the_table(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    c.post(f"/recipes/{rid}/photo", files={"photo": ("a.png", _png(), "image/png")})
    html = c.get(f"/recipes/{rid}").text
    order = [html.index(s) for s in ('class="recipe-thumb"', 'class="servings"', 'class="cfe-wrap"', "Per serving",
                                      "Add to plan", "Edit table", "Change photo")]
    assert order == sorted(order), order
    assert re.search(r'<img[^>]*alt="Brownies"', html)
    name = g.get_object("recipes", rid)["picture_file_name"]
    assert re.search(rf'<button class="recipe-thumb"[^>]*popovertarget="photo-full"', html)
    # without popover support (iOS 16) the thumbnail opens the photo itself and the hidden copy stays hidden
    assert re.search(rf'<button class="recipe-thumb"[^>]*data-href="/recipes/{rid}/photo[?]w=1600&amp;v={name}"', html)
    css = c.get("/static/style.css").text
    assert "[popover] { display: none; }" in css and "[popover]:popover-open { display: block; }" in css
    assert re.search(rf'<div id="photo-full" popover[^>]*>\s*<img src="/recipes/{rid}/photo\?w=1600&amp;v={name}"', html)
    assert html.index('class="recipe-thumb"') < html.index("<h1")  # the photo sits beside the name
    html = c.get(f"/recipes/{brownie_recipe(g)}").text  # no photo: no thumbnail, no broken image
    assert 'class="recipe-thumb"' not in html and "<img" not in html


# ---- cooking aids: where each step happens, cross off as you cook -------------------------------------------------
def test_editor_saves_where(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}/table").text
    assert html.count('name="op_where"') == 6 and "Where" in html  # five steps and the new one
    form = {**chips(BROWNIE), "op_where": ["in a small pot", "same pot", "", "", "in an 8x8 pan"]}
    assert c.post(f"/recipes/{rid}/table", data=form, follow_redirects=False).status_code == 303
    saved = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    assert [o.where for o in saved.ops] == ["in a small pot", "same pot", "", "", "in an 8x8 pan"]
    html = c.get(f"/recipes/{rid}").text
    assert '<span class="cfe-where">in a small pot</span>' in html
    html = c.post(f"/recipes/{rid}/table", data={**form, "action": "remove:0"}).text  # wheres follow their steps
    assert re.findall(r'name="op_where" value="([^"]*)"', html) == ["same pot", "", "", "in an 8x8 pan", ""]


def test_recipe_page_cross_off_hooks(env):
    g, ai, store, c, _ = env
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}").text
    key = re.search(r'data-cross-key="(cross:\d+:[0-9a-f]{8})"', html)[1]
    assert key.startswith(f"cross:{rid}:")
    assert f'data-cross-key="{key}"' in c.get(f"/recipes/{rid}?servings=7").text  # same table, same crossings
    form = chips(BROWNIE)
    form["op_label"][0] = "brown"
    c.post(f"/recipes/{rid}/table", data=form)
    assert f'data-cross-key="{key}"' not in c.get(f"/recipes/{rid}").text  # an edited table starts clean
    cells = re.findall(r'<td class="cfe-(ingredient|op)"[^>]*data-cross="(\w+)"[^>]*>', html)
    assert len(cells) == 9 + 5 and ("ingredient", "i0") in cells and ("op", "o5") in cells
    for td in re.findall(r'<td class="cfe-(?:ingredient|op)"[^>]*>', html):
        assert 'role="button"' in td and 'tabindex="0"' in td and 'aria-pressed="false"' in td
    assert re.search(r'<button[^>]*id="start-over"[^>]*>Start over</button>', html)
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"})
    review = c.get(f"/import/{store.all()[0].id}").text  # the review table is for checking, not cooking
    assert "data-cross" not in review and "Start over" not in review


# ---- layouts that grow with the screen --------------------------------------------------------------------------
def test_review_has_two_panes_and_serves_the_pdf_source(env):
    g, ai, store, c, _ = env
    ai.draft = soup_draft()
    c.post("/import", data={"urls": "https://example.com/soup"},
           files=[("files", ("soup.pdf", b"%PDF-1 soup", "application/pdf"))])
    pdf = next(p for p in store.all() if p.source == "soup.pdf")
    url = next(p for p in store.all() if p.source != "soup.pdf")
    html = c.get(f"/import/{pdf.id}").text
    assert 'class="review-panes two"' in html and "style=" not in html
    src, main = html.index('class="review-source"'), html.index('class="review-main"')
    assert src < main < html.index('name="product_0"') and html.index("Save recipe") > main
    assert re.search(rf'<details class="source-pdf"[^>]*>\s*<summary>Show the source</summary>\s*'
                     rf'<iframe[^>]*src="/import/{pdf.id}/source[#"]', html)
    r = c.get(f"/import/{pdf.id}/source")
    assert r.status_code == 200 and r.content == b"%PDF-1 soup"
    assert r.headers["content-type"] == "application/pdf" and r.headers["content-disposition"].startswith("inline")
    css = c.get("/static/style.css").text  # the frame only from 900 px; phones and tablets get the new-tab link
    assert ".source-pdf iframe { display: none;" in css
    assert ".source-pdf iframe { display: block; }" in css[css.index("@media (min-width: 900px)"):]
    assert re.search(rf'<a href="/import/{pdf.id}/source" target="_blank"', html)
    assert c.get(f"/import/{url.id}/source").status_code == 404  # a website has no file to show
    assert c.get("/import/..%2Fpending%2Fx/source").status_code == 404
    html = c.get(f"/import/{url.id}").text  # no PDF, no photo: one column, the website one click away
    assert 'class="review-panes"' in html and "<iframe" not in html
    assert re.search(r'<a class="btn"[^>]*href="https://example.com/soup"[^>]*target="_blank"', html)


def test_review_asks_claude_for_matches_once(env):
    g, ai, store, c, _ = env
    egg = product(g, "Egg", "piece")  # "dragon egg" is no close name match: Claude picks it
    ai.picks = Picks(items=[IngredientPick(index=1, product_id=egg)])
    asked = []
    real = ai.match_ingredients
    ai.match_ingredients = lambda names, products: asked.append(names) or real(names, products)
    p = store.new("https://example.com/soup")
    store.save(p.model_copy(update={"status": "ready", "draft": soup_draft()}))
    first, again = c.get(f"/import/{p.id}").text, c.get(f"/import/{p.id}").text
    assert asked == [["flour", "dragon egg"]]  # a reload costs nothing
    assert again == first and re.search(rf'<option value="{egg}" selected', again)
    d = soup_draft()  # re-read with another ingredient: only the new name goes to Claude
    d.table.ingredients.append(Ingredient(qty=1, unit="", name="unicorn dust"))
    store.save(store.get(p.id).model_copy(update={"draft": d}))
    c.get(f"/import/{p.id}")
    assert asked[1:] == [["unicorn dust"]]


def test_import_page_has_a_drop_zone_for_pdfs(env):
    html = env[3].get("/import").text
    assert re.search(r'<label class="drop"[^>]*>.*?<input type="file" name="files" accept="application/pdf" multiple',
                     html, re.S)


def test_plan_week_is_a_seven_day_grid(env):
    g, _, _, c, _ = env
    html = c.get("/plan").text
    grid = html[html.index('<div class="week-grid">'):]
    assert grid.count('<section class="plan-day') == 7 and "style=" not in html
    css = c.get("/static/style.css").text
    wide = css[css.index("@media (min-width: 1280px)"):]
    assert ".week-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));" in wide  # 1280-1599
    widest = css[css.index("@media (min-width: 1600px)"):]  # all seven only where "Choose a recipe" still fits
    assert ".week-grid { grid-template-columns: repeat(7, minmax(0, 1fr)); }" in widest


def test_wide_pages_split_into_columns(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}").text
    assert html.index('class="cfe-wrap"') < html.index('class="recipe-more"') < html.index('class="recipe-side"') \
        < html.index("Add to plan")
    assert html.index('class="servings"') < html.index("</h1>") + 200 < html.index('class="cfe-wrap"')  # in the title row
    for url in ("/shopping", "/"):
        assert '<div class="split">' in c.get(url, cookies={"who": "kitchen"}).text, url
    for url in ("/home", "/home/supplies", "/pantry", "/me"):
        assert '<main class="roomy">' in c.get(url, cookies={"who": "kitchen"}).text, url
