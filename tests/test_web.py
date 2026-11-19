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


def test_tabs_have_no_me(env):
    html = env[3].get("/recipes").text
    assert _tabs(html) == [("/", "Today"), ("/plan", "Meals"), ("/inventory", "Inventory"), ("/home", "Chores")]
    assert 'href="/me"' not in html
    assert 'href="/plan" aria-current="page"' in html
    assert 'href="/home" aria-current="page"' in env[3].get("/home").text


def test_meals_switcher_has_three_pages(env):
    html = env[3].get("/recipes").text
    assert [(h, t) for h, _, t in _switcher(html, "Meals pages")] == [("/plan", "Plan"), ("/recipes", "Recipes"),
                                                                     ("/shopping", "Shopping")]
    assert [t for h, a, t in _switcher(html, "Meals pages") if 'aria-current="page"' in a] == ["Recipes"]
    assert 'href="/plan" aria-current="page"' in html and "subnav" not in html and "has-subnav" not in html


def test_scan_button_only_on_inventory_pages(env):
    c = env[3]
    row = re.compile(r'<div class="title-row"><h1[^>]*>.*?</h1>\s*<a class="button title-action" href="([^"]+)">'
                     r'.*?barcode-scan.*?Scan</a>\s*<a class="settings-btn" href="/settings" aria-label="Settings".*?</a></div>', re.S)
    html = c.get("/inventory").text
    assert row.findall(html) == ["/scan"] and html.count("<h1") == 1
    for url in ["/plan", "/recipes", "/shopping", "/import", "/home", "/home/chores/new", "/scan", "/scan?from=chores"]:
        html = c.get(url).text
        assert "title-action" not in html and html.count("<h1") == 1, url
    c.cookies.set("who", "kitchen")
    for url in ["/", "/settings"]:
        html = c.get(url).text
        assert "title-action" not in html and html.count("<h1") == 1, url
    for url in ["/plan", "/home", "/static/style.css", "/static/app.js"]:
        text = c.get(url).text
        assert ".fab" not in text and 'class="fab"' not in text and '"away"' not in text, url


def test_rail_has_no_scan_entries(env):
    c = env[3]
    for url in ["/plan", "/inventory", "/home"]:  # the scanner itself has no rail (full screen)
        rail = c.get(url).text
        rail = rail[rail.index('class="rail"'):rail.index("</nav>")]
        assert "/scan" not in rail and "barcode-scan" not in rail, url


def test_scan_page_belongs_to_inventory(env):
    c = env[3]
    for url in ("/scan", "/scan?from=chores"):  # from=chores is still accepted
        html = c.get(url).text
        assert 'class="segmented pages"' not in html and 'data-section="inventory"' in html, url
        assert re.search(r'<a class="scan-close" href="/inventory" aria-label="Close scanner">', html), url


def test_scan_page_full_screen_no_tabs(env):
    c = env[3]
    html = c.get("/scan").text
    for chrome in ('class="rail"', 'class="title-row"', 'class="settings-btn"', 'class="rail-settings"'):
        assert chrome not in html, chrome
    assert '<main class="scan">' in html and html.count("<h1") == 1
    assert 'class="rail"' in c.get("/inventory").text and 'class="title-row"' in c.get("/inventory").text
    css = c.get("/static/style.css").text
    rule = css[css.index("main.scan {"):]
    assert "position: fixed" in rule[:rule.index("}")] and "inset: 0" in rule[:rule.index("}")]


def _section(html, start, end="</"):
    s = html[html.index(start):]
    return s[:s.index(end)]


def test_scan_page_has_sheet_stepper_trip_and_close(env):
    html = env[3].get("/scan").text
    sheet = _section(html, '<dialog id="sheet"', "</dialog>")
    assert 'class="sheet"' in sheet
    loading = _section(sheet, 'data-state="loading"', "</section>")
    assert 'role="status"' in loading and "Looking up…" in loading and 'class="shimmer' in loading
    found = _section(sheet, '<form id="found"', "</form>")
    assert 'aria-label="One less"' in found and 'aria-label="One more"' in found
    assert re.search(r'<input id="packs" name="packs" type="number" min="1" step="1"', found)
    assert 'name="place"' in found and 'name="amount"' in found and "Keep at least" in found
    assert 'id="found-change"' in found and ">Change<" in found and ">Cancel<" in found
    assert 'id="found-left"' in found and '$("found-left").textContent = d.left;' in env[3].get("/static/app.js").text
    assert 'id="found-error" role="alert"' in found
    unknown = _section(sheet, 'data-state="unknown"', "</section>")
    assert "Take a photo of the package" in unknown and "Fill in the details" in unknown
    assert 'capture="environment"' in unknown
    assert '<form id="manual"' in sheet and "Save product" in sheet
    assert "Try again" in _section(sheet, 'data-state="error"', "</section>")
    trip = _section(sheet, 'data-state="trip"', "</section>")
    assert "This trip" in trip and 'id="trip-list"' in trip
    assert re.search(r'<button class="scan-trip" id="trip" type="button" hidden>', html)
    assert "Add to stock" in html and "Use up" in html and "Open scanner in browser" in html
    assert '<dialog id="fix"' in html and '<dialog id="ask"' not in html
    js = env[3].get("/static/app.js").text
    for bit in ("navigator.vibrate?.(60)", "/api/scan/lookup", "/api/scan/confirm", "/api/scan/undo",
                "sessionStorage", "video.pause()", ".stop()", "decodeFromVideoElement"):
        assert bit in js, bit
    assert '"/api/scan"' not in js and "prompt(" not in js
    css = env[3].get("/static/style.css").text
    rule = css[css.index("dialog.sheet {"):]
    rule = rule[:rule.index("}")]
    assert "max-height: 70dvh" in rule and "overflow-y: auto" in rule
    rule = css[css.index(".stepper button {"):]
    assert "min-height: 56px" in rule[:rule.index("}")]
    motion = css[css.index("@media (prefers-reduced-motion: reduce) { .shimmer"):]
    assert "display: none" in motion[:motion.index("}")]


def test_scan_page_no_inline_styles(env):
    html = env[3].get("/scan").text
    assert "style=" not in html and "<style" not in html and "<script>" not in html
    js = env[3].get("/static/app.js").text
    assert ".style." not in js and "innerHTML" not in js


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


def test_me_redirects_to_settings(env):
    r = env[3].get("/me", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"


def test_settings_page_rows(env):
    c = env[3]
    c.cookies.set("who", "kitchen")
    html = c.get("/settings").text
    assert 'href="/settings" aria-label="Settings" aria-current="page"' in html and "aria-current" not in _tabs_ul(html)
    assert 'href="/who"' in html and 'href="/home/people"' in html and "AI this month" in html
    assert "kitchen screen" in html.lower() and 'class="rows"' in html
    assert re.findall(r'name="theme" value="(\w+)"[^>]*>([^<]+)<', html) == [
        ("dark", "Midnight"), ("black", "Black"), ("light", "Light"), ("auto", "Match phone")]
    assert re.search(r'value="dark"[^>]*aria-pressed="true"', html)
    assert 'aria-current="page"' not in _tabs_ul(c.get("/who").text) and 'href="/settings" aria-label="Settings" aria-current' in c.get("/who").text
    assert 'href="/settings" aria-label="Settings" aria-current' in c.get("/home/people").text


def _tabs_ul(html):
    return html[html.index('class="rail"'):html.index('<a class="rail-settings"')]


def test_settings_button_on_every_section(env):
    for url in ("/", "/plan", "/inventory", "/home"):
        html = env[3].get(url, cookies={"who": "kitchen"}).text
        assert 'href="/settings" aria-label="Settings"' in html and 'class="rail-settings" href="/settings"' in html, url


def test_theme_values(env):
    c = env[3]
    assert 'data-theme="dark"' in c.get("/settings").text
    for value in ("dark", "black", "light", "auto"):
        r = c.post("/settings/theme", data={"theme": value}, headers={"referer": "http://testserver/settings"},
                   follow_redirects=False)
        assert r.headers["location"] == "/settings" and f"theme={value}" in r.headers["set-cookie"]
        html = c.get("/settings").text
        assert f'data-theme="{value}"' in html and re.search(rf'value="{value}"[^>]*aria-pressed="true"', html)
    assert c.post("/settings/theme", data={"theme": "bogus"}).status_code == 422
    c.cookies.clear()
    c.cookies.set("theme", "bogus")
    assert 'data-theme="dark"' in c.get("/settings").text


def test_black_theme_meta_color(env):
    c = env[3]
    meta = lambda html: re.findall(r'<meta name="theme-color" content="([^"]+)"([^>]*)>', html)
    assert meta(c.get("/", cookies={"theme": "black", "who": "kitchen"}).text) == [("#000000", "")]
    assert meta(c.get("/", cookies={"theme": "light", "who": "kitchen"}).text) == [("#E2E5EA", "")]
    assert meta(c.get("/", cookies={"who": "kitchen"}).text) == [("#2E333A", "")]
    assert meta(c.get("/", cookies={"theme": "auto", "who": "kitchen"}).text) == [
        ("#E2E5EA", ' media="(prefers-color-scheme: light)"'), ("#000000", ' media="(prefers-color-scheme: dark)"')]


def test_chores_page_sections(env):
    html = env[3].get("/home").text
    assert "<h1>Chores</h1>" in html and "<h2>Repeating</h2>" in html and "<h2>Chores</h2>" not in html


def test_no_footer_theme_button(env):
    for url in ["/", "/inventory", "/home"]:
        assert "theme-switch" not in env[3].get(url).text, url


def test_theme_cookie_switches_to_light(env):
    c = env[3]
    assert 'data-theme="dark"' in c.get("/inventory").text
    r = c.post("/settings/theme", data={"theme": "light"}, headers={"referer": "http://testserver/inventory"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inventory"
    assert "max-age=34560000" in r.headers["set-cookie"].lower()
    assert 'data-theme="light"' in c.get("/inventory").text
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
    for url in ["/inventory", "/scan", "/recipes", f"/recipes/{rid}", f"/recipes/{rid}/table", "/import",
                f"/import/{pid}", "/static/style.css", "/static/app.js", "/static/manifest.webmanifest"]:
        assert c.get(url).status_code == 200, url
    assert c.get("/recipes/999").status_code == 404 and c.get("/import/" + "0" * 32).status_code == 404


def test_base_has_pwa_bits_and_tabs_ul(env):
    html = env[3].get("/plan").text
    for bit in ['name="viewport"', 'rel="manifest"', 'apple-touch-icon', 'apple-mobile-web-app-capable" content="yes"',
                'href="/plan"', 'href="/inventory"', 'href="/recipes"', 'href="/shopping"']:
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
    assert '<span class="cfe-amt">8 oz</span> unsalted butter' in html and 'value="4"' in html


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


def test_scan_photo_creates_the_product(env):
    g, ai, _, c, _ = env
    ai.guess = ProductGuess(generic_name="Beans", is_new_generic=True, stock_unit="g", package_amount=400,
                            location="Pantry", best_before_days=700, g_per_unit=1)
    r = c.post("/api/scan/photo", data={"code": "555"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "found" and r["created"] and r["generic"] == "Beans" and r["package"] == 400
    assert g.product_by_barcode("555")["product"]["id"] == r["product_id"]
    assert r["product_id"] not in g.stock  # the sheet confirms before anything is added


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
    html = c.get("/inventory").text
    assert html.index("Use soon") < html.index("<h2>Fridge</h2>") and html.count("Yogurt") >= 2
    assert c.post(f"/pantry/{pid}/consume", data={"amount": 100}, follow_redirects=False).headers["location"] == "/inventory"
    assert g.stock[pid][0]["amount"] == 400
    c.post(f"/pantry/{pid}/move", data={"from_location": i.location["Fridge"], "to_location": i.location["Freezer"],
                                        "amount": 400})
    assert g.stock[pid][0]["location_id"] == i.location["Freezer"]
    c.post(f"/pantry/{pid}/amount", data={"amount": 250})
    assert sum(e["amount"] for e in g.stock[pid]) == 250


def review_form(**extra):
    base = {"product_0": "", "new_name_0": "flour", "product_1": "", "new_name_1": "dragon egg",
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
    assert "2 cup flour" in html and "dragon egg" in html and "1 cup =" not in html  # flour g: Claude estimates
    ai.conversion = ConversionGuess(factor=125, explanation="")
    r = c.post(f"/import/{pdf.id}", data=review_form(product_0=flour, product_1=egg), follow_redirects=False)
    url = next(p for p in pend if p.source.startswith("https"))
    assert r.status_code == 303 and r.headers["location"] == f"/import/{url.id}"  # the other import is next
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"]) for p in pos] == [(flour, 250), (egg, 1)]
    assert store.get(pdf.id) is None and store.pdf_path(pdf.id) is None
    assert (tmp / "sources" / f"{rid}.pdf").read_bytes() == b"%PDF-1"
    assert c.post(f"/import/{url.id}/discard", follow_redirects=False).status_code == 303
    assert store.all() == []


def test_review_create_and_errors(env):
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
    assert f'<option value="{egg}" selected' in html and 'value="create:0"' in html and 'value="create:1"' not in html
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg, op_in_0=["i0"])).text
    assert "“dragon egg” isn&#39;t in any step yet" in html and g.objects("recipes") == []
    assert f'<option value="{flour}" selected' in html and store.get(pid) is not None  # the picks stay


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
    assert re.search(r'value="o1"[^>]*>\s*<span>whisk</span>', pages[0])  # earlier step chip (renumbered), by label


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
    ai.conversion = ConversionGuess(factor=125, explanation="")
    r = c.post(f"/import/{pid}", data={**review_form(product_0=flour, product_1=egg), **chips(t)},
               follow_redirects=False)
    assert r.status_code == 303
    rid = max(int(x["id"]) for x in g.objects("recipes"))
    saved = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
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


def test_grocy_refusal_says_so_with_its_reason(tmp_path):
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    exc = GrocyError("POST /objects/chores -> 400: {...}", 400, "table chores has no column named x")
    app = create_app(DownGrocy(exc), FakeAI(), httpx.Client(transport=httpx.MockTransport(no_network)), settings,
                     PendingStore(tmp_path / "pending"))
    r = TestClient(app).get("/recipes")
    assert r.status_code == 502 and "Grocy refused that" in r.text and "nothing was saved" in r.text
    assert "Grocy said: table chores has no column named x" in r.text and "Grocy is down" not in r.text


def test_review_packet_ingredient_saves_its_measured_amount(env):
    g, ai, store, c, _ = env
    sour = product(g, "Sour cream")
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="tbsp", name="sour cream", note="(est.)", packets=1)],
                 ops=[Op(id="o1", label="mix", inputs=["i0"])])
    ai.draft = RecipeDraft(title="Tacos", servings=2, steps=["Stir"], table=t)
    ai.conversion = ConversionGuess(factor=15, explanation="")
    calls = counting_conversions(ai)
    c.post("/import", data={"urls": "https://example.com/tacos"})
    pid = store.all()[0].id
    form = dict(product_0=sour, new_name_0="sour cream", op_id=["o1"], op_label=["mix"], op_in_0=["i0"])
    html = c.post(f"/import/{pid}", data={**form, "action": "update"}).text
    assert 'class="error"' not in html
    assert re.search(r'2 tbsp sour cream \(est\.\)\s*<span class="packet">1 packet</span>', html)  # the review row
    r = c.post(f"/import/{pid}", data={**form, "action": "save"}, follow_redirects=False)
    assert r.status_code == 303 and [a[1:] for a in calls] == [("tbsp", "g")]  # the measured amount, not the packet
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    [pos] = g.objects("recipes_pos", recipe_id=rid)
    assert pos["amount"] == 30 and not pos.get("variable_amount")  # taken out of stock when cooked
    assert pos["note"] == "2 tbsp sour cream (est.)"
    [ing] = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients
    assert (ing.qty, ing.unit, ing.packets) == (2, "tbsp", 1)


def test_recipe_page_hides_steps_when_table_exists(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    html = c.get(f"/recipes/{rid}").text
    assert 'class="cfe"' in html and "<summary>Steps</summary>" not in html and "<li>Mix</li>" not in html
    g.set_userfields("recipes", rid, {"cfe_table": ""})  # no table: the steps show
    html = c.get(f"/recipes/{rid}").text
    assert "<h2>Steps</h2>" in html and "<li>Mix</li>" in html


def test_table_editor_keeps_packets(env):
    g, _, _, c, _ = env
    rid = brownie_recipe(g)
    t = BROWNIE.model_copy(update={"ingredients": [i.model_copy(update={"packets": 1 if n == 6 else None})
                                                   for n, i in enumerate(BROWNIE.ingredients)]})
    g.set_userfields("recipes", rid, {"cfe_table": t.model_dump_json()})
    html = c.get(f"/recipes/{rid}/table").text
    fields = re.findall(r'<input name="packets"[^>]*>', html)
    assert len(fields) == len(BROWNIE.ingredients) and 'value="1"' in fields[6] and 'value=""' in fields[0]
    assert 'aria-label="Packets of cocoa powder"' in fields[6]

    def saved():
        return [i.packets for i in CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients]

    assert c.post(f"/recipes/{rid}/table", data=chips(t), follow_redirects=False).status_code == 303
    assert saved() == [None] * 6 + [1, None, None]  # a form without the fields keeps them
    packets = [""] * 9
    packets[1], packets[6] = "2", "1.5"
    r = c.post(f"/recipes/{rid}/table", data={**chips(t), "packets": packets}, follow_redirects=False)
    assert r.status_code == 303 and saved() == [None, 2, None, None, None, None, 1.5, None, None]
    packets[1], packets[6] = "", "nonsense"
    c.post(f"/recipes/{rid}/table", data={**chips(t), "packets": packets})
    assert saved() == [None] * 9


def test_review_estimates_belong_to_their_product(env):
    g, ai, store, c, _ = env
    flour, sugar, egg = product(g, "Flour"), product(g, "Sugar"), product(g, "Dragon egg", "piece")
    ai.draft = soup_draft()
    ai.conversion = ConversionGuess(factor=125, explanation="")
    calls = counting_conversions(ai)
    c.post("/import", data={"urls": "https://example.com/soup"})
    pid = store.all()[0].id
    c.post(f"/import/{pid}", data=review_form(product_0=flour))  # egg not picked: not saved, flour estimated
    ai.conversion = ConversionGuess(factor=200, explanation="")
    r = c.post(f"/import/{pid}", data=review_form(product_0=sugar, product_1=egg), follow_redirects=False)
    assert r.status_code == 303 and [a[0] for a in calls] == ["Flour", "Sugar"]
    assert not g.objects("quantity_unit_conversions", product_id=flour)  # its estimate went unused
    assert [x["factor"] for x in g.objects("quantity_unit_conversions", product_id=sugar)] == [200]
    assert sorted(p["amount"] for p in g.objects("recipes_pos")) == [1, 400]


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
    ai = FakeAI(picks=Picks(items=[]), draft=soup_draft(), conversion=ConversionGuess(factor=125, explanation=""))
    app = create_app(g, ai,
                     httpx.Client(transport=httpx.MockTransport(no_network)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), store)
    form = review_form(product_0=flour, product_1=egg)
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
        assert c.get("/inventory").status_code == 200 and time.monotonic() - t0 < 2  # loop not blocked by the save
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
    assert "You were short on: Flour" in r.text and 'href="/inventory"' in r.text
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
    assert c.get("/inventory", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
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


def _barcoded(g, name="Brand Rice", code="321", **kw):
    pid = product(g, name, parent_product_id=product(g, name.split()[-1]), **kw)
    g.create("product_barcodes", {"product_id": pid, "barcode": code})
    return pid


def test_use_with_amount_is_not_remembered(env):
    g, _, _, c, _ = env
    pid = _barcoded(g)
    g.add_stock(pid, 500, None, None)
    assert c.post("/api/scan/confirm", json={"product_id": pid, "action": "use", "amount": 120}).json()["amount"] == 120
    assert "package_amount" not in g.userfields("products", pid) and g.stock_amounts()[pid] == 380


def test_supply_used_one_takes_one_unit(env):
    g, _, _, c, _ = env
    bags = supply(g, stock=64, package_amount=32)
    c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 63


def test_scan_use_on_supply_takes_one_unit(env):
    g, _, _, c, _ = env
    bags = supply(g, stock=64, package_amount=32)
    r = c.post("/api/scan/confirm", json={"product_id": bags, "action": "use"}).json()
    assert r["status"] == "ok" and r["amount"] == 1 and g.stock_amounts()[bags] == 63
    bare = supply(g, "Sponges", stock=5)  # no package size: still never asks for an amount
    r = c.post("/api/scan/confirm", json={"product_id": bare, "action": "use"}).json()
    assert r["status"] == "ok" and g.stock_amounts()[bare] == 4


def test_scan_use_on_food_still_takes_a_package(env):
    g, _, _, c, _ = env
    pid = _barcoded(g, "Brand Beans", "777")
    g.set_userfields("products", pid, {"package_amount": 400})
    g.add_stock(pid, 1200, None, None)
    assert c.post("/api/scan/confirm", json={"product_id": pid, "action": "use"}).json()["amount"] == 400
    assert g.stock_amounts()[pid] == 800


def test_bought_without_package_size_adds_row_amount(env):
    g, _, _, c, _ = env
    pid, rid = flour_recipe(g)  # no package_amount
    c.post("/plan", data={"day": _future_day(1).isoformat(), "section": "Dinner", "recipe_id": rid})  # needs 300 g
    [row] = g.objects("shopping_list", shopping_list_id=ids(g).list["Meal plan"])
    c.post(f"/shopping/{row['id']}/toggle")
    c.post("/shopping/bought")
    assert g.stock_amounts()[pid] == 300


def test_inventory_machine_read_amounts_are_not_rounded_up(env):
    g, _, _, c, _ = env
    pid = product(g, "Rice")
    g.add_stock(pid, 2189.1366, None, ids(g).location["Pantry"])
    html = c.get("/inventory").text
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


def test_who_and_people_live_in_settings(hv):
    c = hv[3]
    assert 'href="/settings" aria-label="Settings" aria-current="page"' in c.get("/who").text
    people = c.get("/home/people").text
    assert 'aria-label="Settings" aria-current="page"' in people and 'href="/home" aria-current' not in people


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
    r = c.post("/api/scan/photo", data={"code": "4242"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["status"] == "found" and r["need_min"] and r["label"] == "pieces" and r["package"] == 40
    pid = r["product_id"]
    assert pid not in g.stock
    for bad in (0, -1, "inf"):  # the sheet's Keep at least goes with the add
        assert c.post("/api/scan/confirm", json={"product_id": pid, "action": "add", "min": bad}).status_code == 422
    assert pid not in g.stock
    r = c.post("/api/scan/confirm", json={"product_id": pid, "action": "add", "min": 2}).json()
    assert r["status"] == "ok" and r["amount"] == 40 and g.stock_amounts()[pid] == 40
    generic = g.get_object("products", g.get_object("products", pid)["parent_product_id"])
    assert generic["min_stock_amount"] == 2 and generic["cumulate_min_stock_amount_of_sub_products"] == 1
    assert "min_stock_amount" not in g.get_object("products", pid)  # kept on the generic, not the brand
    assert g.stock[pid][0]["best_before_date"] is None  # never expires
    assert c.post("/api/scan/lookup", json={"code": "4242"}).json()["need_min"] is False  # asked once only
    ai.guess = ai.guess.model_copy(update={"is_new_generic": False})  # a second brand of the same generic
    r = c.post("/api/scan/photo", data={"code": "4343"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert not r["need_min"] and r["product_id"] != pid  # once per generic, not per brand


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
    html = c.get("/inventory").text
    assert "Trash bags" in html and "Sponges" in html and "Glad bags" not in html and "Hefty bags" not in html
    assert 'Trash bags</b><span class="qty">5 left</span>' in html  # its brands' stock, one row
    assert 'Sponges</b><span class="qty">3 left</span>' in html and "pieces</span>" in html
    assert f'action="/home/supplies/{sponge}/used"' in html and "Used one" in html and "Keep at least" in html
    assert f'/home/supplies/{glad}/' not in html
    r = c.post(f"/home/supplies/{bags}/used", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inventory?msg=Used+one+Trash+bags."
    assert g.stock_amounts()[bags] == 4  # no package size: one unit, taken from a brand
    g.set_userfields("products", bags, {"package_amount": 10})
    c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 3  # a known package size no longer matters: still one unit
    for _ in range(3):
        c.post(f"/home/supplies/{bags}/used")
    assert g.stock_amounts()[bags] == 0
    assert c.post(f"/home/supplies/{bags}/used").status_code == 200  # nothing left: no error
    html = c.get("/inventory").text
    assert 'Trash bags</b><span class="qty">None left</span>' in html
    assert '<p class="stock-when warn">Running low.</p>' in html
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
    pages = {url: c.get(url).text for url in ("/inventory", "/scan", "/import", f"/import/{pid}", f"/recipes/{rid}/table")}
    pages["down"] = down.text
    for url, html in pages.items():
        section = "inventory" if url in ("/inventory", "/scan") else "kitchen"
        assert f'data-section="{section}"' in html and "style=" not in html, url
        assert "·" not in html and "→" not in html, url  # no middle dots or arrows in copy


def test_inventory_food_rows_read_like_supplies(env):
    g, _, _, c, _ = env
    i = ids(g)
    eggs = product(g, "Eggs", "piece")
    g.add_stock(eggs, 12, date.today() + timedelta(days=2), i.location["Fridge"])
    rice = product(g, "Rice")
    g.add_stock(rice, 500, None, i.location["Pantry"])
    supply(g, "Toilet paper", stock=6)
    g.volatile_result["due_products"] = [{"product_id": eggs, "amount": 12,
                                          "best_before_date": (date.today() + timedelta(days=2)).isoformat()}]
    html = c.get("/inventory").text
    assert html.index("<h2>Garage</h2>") < html.index("Toilet paper")  # supplies share the page, by place
    assert "12 left" in html and "500 g left" in html and "12 piece" not in html
    day = f"{date.today() + timedelta(days=2):%A}"
    assert f"Use by {day}" in html and f"Best before {day}" in html


def test_scan_sheet_saves_on_enter(env):
    html = env[3].get("/scan").text
    fix = _section(html, '<dialog id="fix"', "</dialog>")
    assert fix.index('value="ok"') < fix.index('value="cancel"')  # Enter submits the first button
    for form in ('<form id="found"', '<form id="manual"'):
        f = _section(html, form, "</form>")
        submits = [b for b in re.findall(r"<button[^>]*>", f) if 'type="button"' not in b]
        assert len(submits) == 1 and "btn-primary" in submits[0], form  # the phone's Go confirms

def test_typed_pack_size_reaches_running_low_bought(env):
    """Running low names the generic, so a size typed for a new brand is kept on its generic too."""
    g, ai, _, c, _ = env
    bags = supply(g)  # keep at least 2, no package size
    ai.guess = ProductGuess(generic_name="Trash bags", is_new_generic=False, stock_unit="piece", package_amount=None,
                            location="Garage", best_before_days=-1, g_per_unit=20, category="supply")
    r = c.post("/api/scan/photo", data={"code": "5151"},
               files={"image": ("p.jpg", b"\xff\xd8jpeg", "image/jpeg")}).json()
    assert r["package"] is None
    assert c.post("/api/scan/confirm", json={"product_id": r["product_id"], "action": "add",
                                             "amount": 32}).json()["status"] == "ok"
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
    assert r.headers["content-type"] == "image/jpeg" and r.headers["x-content-type-options"] == "nosniff"
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
    assert ".week-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));" in wide  # 1280-1699
    widest = css[css.index("@media (min-width: 1700px)"):]  # all seven only where "Choose a recipe" still fits
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
    for url in ("/home", "/inventory", "/settings"):
        assert '<main class="roomy">' in c.get(url, cookies={"who": "kitchen"}).text, url


def test_black_theme_css_covers_every_surface_and_inks_on_action(env):
    css = env[3].get("/static/style.css").text
    black = css[css.index('[data-theme="black"], [data-theme="auto"] {'):]
    black = black[:black.index("}")]
    assert "--on-action: #000" in black and "--line: rgba(255,255,255,.45)" in black
    outline = css[css.index(':is([data-theme="black"], [data-theme="auto"]) :is('):]
    outline = outline[:outline.index("{")]
    for sel in ("table.cfe td", "td.cfe-filler", "td.cfe-op", ".recipe-grid img", ".photo-none", ".review-photo",
                ".recipe-thumb"):
        assert sel in outline, sel
    for sel in (".rail-settings[aria-current]", ".settings-btn[aria-current]", ".rail > ul > li > a[aria-current]"):
        assert f"{sel}" in css[css.index("The current tab"):], sel
    assert ") a.settings-btn[aria-current][aria-current] { border-radius: 24px; }" in css  # outranks the :is() rule
    assert "color: #fff" not in css  # ink on an action fill comes from --on-action


# ---- the review asks only for products ------------------------------------------------------------------------------
def ready_import(store, draft, age=0):
    """A pending import already read, `age` seconds older than now (store.all() is oldest first by file time)."""
    import os
    p = store.new("https://example.com/x")
    p.status, p.draft = "ready", draft
    store.save(p)
    t = time.time() - age
    os.utime(store.dir / f"{p.id}.json", (t, t))
    return p.id


def counting_conversions(ai):
    calls, real = [], ai.guess_conversion
    ai.guess_conversion = lambda *a: calls.append(a) or real(*a)
    return calls


def test_review_shows_no_amounts_or_factors(env):
    g, ai, store, c, _ = env
    product(g, "Flour"), product(g, "Sour cream")  # grams, no cup conversion known
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"),
                                      Ingredient(qty=1, unit="packet", name="sour cream")],
                 ops=[Op(id="o1", label="mix", inputs=["i0", "i1"])])
    pid = ready_import(store, RecipeDraft(title="Tacos", servings=2, steps=["Stir"], table=t))
    html = c.get(f"/import/{pid}").text
    visible = re.sub(r"<[^>]*>", " ", html)
    assert "Uses" not in visible and "1 cup =" not in visible and "Suggest" not in visible
    assert 'name="amount_' not in html and 'name="factor_' not in html
    assert "Packet sizes vary" not in visible and "enter the real amount" not in visible


def test_review_saves_with_only_products_picked(env):
    g, ai, store, c, _ = env
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="a cup of flour weighs about 125 g")
    calls = counting_conversions(ai)
    pid = ready_import(store, soup_draft())
    c.get(f"/import/{pid}"), c.get(f"/import/{pid}")
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1="")).text  # one row not picked yet
    assert "Pick a product for “dragon egg”" in html and g.objects("recipes") == []
    assert store.get(pid).estimates == {f"{flour}:cup": 125}  # kept: the next Save doesn't ask again
    r = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg), follow_redirects=False)
    assert r.status_code == 303 and len(calls) == 1
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"]) for p in pos] == [(flour, 250), (egg, 1)]
    [conv] = g.objects("quantity_unit_conversions", product_id=flour)
    assert (conv["from_qu_id"], conv["to_qu_id"], conv["factor"]) == (ids(g).unit["cup"], ids(g).unit["g"], 125)


def test_review_free_mode_saves_without_amount(env):
    from app.mealplan import cook
    g, ai, store, c, _ = env
    flour, egg, salt = product(g, "Flour"), product(g, "Dragon egg", "piece"), product(g, "Salt")
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"),
                                      Ingredient(qty=1, unit="", name="dragon egg"),
                                      Ingredient(qty=None, unit="", name="salt", note="to taste")],
                 ops=[Op(id="o1", label="mix", inputs=["i0", "i1", "i2"])])
    pid = ready_import(store, RecipeDraft(title="Soup", servings=2, steps=["Stir"], table=t))
    ai.mode_ = "free"
    r = c.post(f"/import/{pid}", data={"product_0": flour, "product_1": egg, "product_2": salt, "op_id": ["o1"],
                                       "op_label": ["mix"], "op_in_0": ["i0", "i1", "i2"], "action": "save"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"], p.get("variable_amount") or "") for p in pos] == \
        [(flour, 0, "2 cup"), (egg, 1, ""), (salt, 0, "")]  # flour waits for an estimate; salt is to taste
    assert not g.objects("quantity_unit_conversions", product_id=flour)
    i = ids(g)
    for p in (flour, egg):
        g.add_stock(p, 500, None, i.location["Pantry"])
    entry = g.create("meal_plan", {"day": "2026-11-19", "type": "recipe", "recipe_id": rid, "recipe_servings": 2,
                                   "section_id": i.section["Dinner"], "done": 0})
    assert cook(g, entry) == []
    assert g.stock_amounts()[flour] == 500 and g.stock_amounts()[egg] == 499  # nothing guessed is taken out


def test_create_all_missing(env):
    from app.ai import AIError
    g, ai, store, c, _ = env
    ai.guess = ProductGuess(generic_name="x", is_new_generic=True, stock_unit="g", package_amount=None,
                            location="Pantry", best_before_days=30, g_per_unit=1)
    real = ai.guess_product

    def guess(info, *a, **kw):
        if info["name"] == "Dragon egg":
            raise AIError("Claude didn't answer")
        return real(info, *a, **kw)
    ai.guess_product = guess
    salt = product(g, "Salt")
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour"),
                                      Ingredient(qty=1, unit="", name="dragon egg"),
                                      Ingredient(qty=1, unit="tsp", name="salt"),
                                      Ingredient(qty=1, unit="", name="leek")],
                 ops=[Op(id="o1", label="mix", inputs=["i0", "i1", "i2", "i3"])])
    pid = ready_import(store, RecipeDraft(title="Soup", servings=2, steps=["Stir"], table=t))
    html = c.get(f"/import/{pid}").text
    assert html.count('value="create_all"') == 1 and html.index('value="create_all"') < html.index('name="product_0"')
    html = c.post(f"/import/{pid}", data={"product_2": salt, "new_name_0": "Flour", "new_name_1": "Dragon egg",
                                          "new_name_3": "Leek", "op_id": ["o1"], "op_label": ["mix"],
                                          "op_in_0": ["i0", "i1", "i2", "i3"], "action": "create_all"}).text
    made = {p["name"]: p["id"] for p in g.objects("products") if p["name"] in ("Flour", "Leek", "Dragon egg")}
    assert list(made) == ["Flour", "Leek"] and made["Flour"] < made["Leek"]  # row order; the egg failed
    for i, p in ((0, made["Flour"]), (2, salt), (3, made["Leek"])):
        assert re.search(rf'name="product_{i}"[^>]*>.*?<option value="{p}" selected', html, re.S), i
    rows = html.split('<li class="review-row')
    assert "Claude didn&#39;t answer" in rows[2] and "Claude didn" not in rows[1] + rows[3]  # on its own row
    assert html.count('value="create:1"') == 1 and 'value="create:0"' not in html


def test_review_waiting_link_opens_first_review(env):
    g, ai, store, c, _ = env
    assert c.get("/import/next", follow_redirects=False).headers["location"] == "/import"
    failed = store.new("https://example.com/broken")
    failed.status, failed.error = "failed", "no recipe"
    store.save(failed)
    assert c.get("/import/next", follow_redirects=False).headers["location"] == "/import"
    newer = ready_import(store, soup_draft(), age=10)
    older = ready_import(store, soup_draft(), age=60)
    html = c.get("/recipes").text
    assert re.search(r'<a class="btn" href="/import/next">Review 3 waiting</a>', html)
    r = c.get("/import/next", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/import/{older}" != f"/import/{newer}"


def test_save_and_discard_go_to_next_waiting(env):
    g, ai, store, c, _ = env
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    first = ready_import(store, soup_draft(), age=60)
    second, third = ready_import(store, soup_draft(), age=30), ready_import(store, soup_draft(), age=10)
    r = c.post(f"/import/{first}", data=review_form(product_0=flour, product_1=egg), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/import/{second}"
    html = c.get(f"/import/{second}").text
    assert '<input type="hidden" name="then" value="next">' in html  # the review's Discard
    r = c.post(f"/import/{second}/discard", data={"then": "next"}, follow_redirects=False)
    assert r.headers["location"] == f"/import/{third}" and store.get(second) is None
    assert c.post(f"/import/{third}/discard", data={"then": "next"}, follow_redirects=False).headers["location"] \
        == "/recipes"
    gone = ready_import(store, soup_draft())
    assert c.post(f"/import/{gone}/discard", follow_redirects=False).headers["location"] == "/import"  # Import list
    failed = store.new("https://example.com/broken")
    store.save(failed.model_copy(update={"status": "failed", "error": "no recipe"}))
    html = c.get(f"/import/{failed.id}").text
    assert "Discard" in html and 'name="then"' not in html  # a failed import's Discard goes back to the list


def test_recipe_page_reads_amount_markers_safely(env):
    g, ai, store, c, _ = env
    flour, salt, egg = product(g, "Flour"), product(g, "Salt"), product(g, "Egg", "piece")
    t = CfeTable(pre=[], ops=[], ingredients=[Ingredient(qty=1, unit="cup", name="flour"),
                                              Ingredient(qty=1, unit="pinch", name="salt"),
                                              Ingredient(qty=2, unit="", name="egg")])
    rid = save_recipe(g, RecipeDraft(title="Bread", servings=2, steps=["Mix"], table=t),
                      [IngredientMatch(0, flour, None), IngredientMatch(1, salt, None), IngredientMatch(2, egg, 2)],
                      None, None, need_table=False)
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    for p, mark in zip(pos, ("1/2 cup", "some", "3 cup")):  # hand-edited in Grocy; the egg has a real amount
        g.update("recipes_pos", p["id"], {"variable_amount": mark})
    r = c.get(f"/recipes/{rid}")
    assert r.status_code == 200 and "1/2 cup Flour" in r.text and "2 Egg" in r.text
    assert "3 cup" not in r.text and "0 g Salt" not in r.text and "some" not in r.text
    assert c.get(f"/recipes/{rid}/table").status_code == 200


def test_free_create_needs_what_it_is_counted_in(env):
    g, ai, store, c, _ = env
    pid = ready_import(store, soup_draft())
    ai.mode_ = "free"
    html = c.get(f"/import/{pid}").text
    assert re.search(r'<select name="stock_unit_0"><option value="" selected>Choose…</option>', html)
    form = {"new_name_0": "Flour", "generic_0": "Flour", "stock_unit_0": "g", "location_0": "Pantry",
            "category_0": "food", "new_name_1": "Dragon egg", "generic_1": "Dragon egg", "stock_unit_1": "",
            "location_1": "Pantry", "category_1": "food", "op_id": ["o1"], "op_label": ["mix"],
            "op_in_0": ["i0", "i1"], "action": "create_all"}
    html = c.post(f"/import/{pid}", data=form).text
    assert [p["name"] for p in g.objects("products")] == ["Flour"]  # the egg wasn't guessed into grams
    rows = html.split('<li class="review-row')
    assert "Choose what it&#39;s counted in." in rows[2] and "Choose what it" not in rows[1]
    html = c.post(f"/import/{pid}", data={**form, "action": "create:1"}).text
    assert "Choose what it&#39;s counted in." in html and len(g.objects("products")) == 1


def test_a_refused_conversion_still_saves_the_recipe(env):
    g, ai, store, c, _ = env
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    real = g.create

    def create(entity, data):
        if entity == "quantity_unit_conversions":
            raise GrocyError("POST -> 400", 400, "refused")
        return real(entity, data)
    g.create = create
    pid = ready_import(store, soup_draft())
    r = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/recipes" and len(g.objects("recipes")) == 1
    assert store.get(pid) is None and not g.objects("quantity_unit_conversions", product_id=flour)


# ---- the accuracy check's doubts (Task 3) ------------------------------------------------------------------------
DOUBTS = [{"ref": "i1", "reason": "The card says 2 dragon eggs."}, {"ref": "o1", "reason": "No time on the card."},
          {"ref": "", "reason": "The card lists salt; no row has it."}]


def doubtful(store, doubts=DOUBTS):
    p = store.new("https://example.com/soup")
    store.save(p.model_copy(update={"status": "ready", "draft": soup_draft(), "doubts": doubts}))
    return p.id


def table_cells(html):
    """(classes, visible words) per table cell."""
    return [(cls, " ".join(re.sub(r"<[^>]*>", " ", body).split()))
            for cls, body in re.findall(r'<td class="([^"]*)"[^>]*>(.*?)</td>', html, re.S)]


def doubt_notes(html):
    m = re.search(r'<div class="doubts" role="note"[^>]*>(.*?)</div>', html, re.S)
    return m and re.findall(r"<li>(.*?)</li>", m.group(1), re.S)


def test_review_marks_doubts_and_lists_reasons(env):
    g, ai, store, c, _ = env
    html = c.get(f"/import/{doubtful(store)}").text
    assert table_cells(html) == [("cfe-ingredient", "2 cup flour"), ("cfe-op doubt", "mix Check this, note 2"),
                                 ("cfe-ingredient doubt", "1 dragon egg Check this, note 1")]
    # numbered as the markers are; the one about no row in particular is listed without marking a cell
    assert doubt_notes(html) == ["The card says 2 dragon eggs.", "No time on the card.",
                                 "The card lists salt; no row has it."]
    assert "Check this against the card" in html and "style=" not in html and "·" not in html
    assert doubt_notes(c.get(f"/import/{doubtful(store, [])}").text) is None  # nothing doubted: no list


def test_editing_a_row_clears_its_doubt(env):
    g, ai, store, c, _ = env
    pid = doubtful(store)
    html = c.post(f"/import/{pid}", data=review_form(action="update")).text  # nothing changed: all stand
    assert len(doubt_notes(html)) == 3
    html = c.post(f"/import/{pid}", data=review_form(action="update", op_label=["mix 2 min", ""])).text
    assert doubt_notes(html) == ["The card says 2 dragon eggs.", "The card lists salt; no row has it."]
    assert ("cfe-op", "mix 2 min") in table_cells(html)
    assert ("cfe-ingredient doubt", "1 dragon egg Check this, note 1") in table_cells(html)
    html = c.post(f"/import/{pid}", data=review_form(action="update", packets=["", "2"])).text  # egg's packets
    assert doubt_notes(html) == ["No time on the card.", "The card lists salt; no row has it."]
    assert store.get(pid).doubts == DOUBTS  # an edit isn't kept until Save, so neither is its clearing
    html = c.post(f"/import/{pid}", data=review_form()).text  # a refused Save (no products picked) keeps them all
    assert len(doubt_notes(html)) == 3 and store.get(pid).doubts == DOUBTS


def test_step_doubts_survive_an_update_with_no_edits(env):
    from app.ai import Doubt, TableCheck
    g, ai, store, c, _ = env
    t = CfeTable(pre=[], ingredients=soup_draft().table.ingredients,
                 ops=[Op(id="whisk", label="whisk", inputs=["i0"]), Op(id="fold", label="fold in", inputs=["whisk", "i1"])])
    ai.draft = RecipeDraft(title="Soup", servings=4, steps=["Stir"], table=t)
    ai.check = TableCheck(table=t, doubts=[Doubt(ref="fold", reason="No time on the card."),
                                           Doubt(ref="whisk", reason="Whisk how long?")])
    c.post("/import", data={"urls": "https://example.com/soup"})
    p = store.all()[0]
    html = c.post(f"/import/{p.id}", data={**chips(p.draft.table), "action": "update"}).text
    assert doubt_notes(html) == ["No time on the card.", "Whisk how long?"]
    assert ("cfe-op doubt", "fold in Check this, note 1") in table_cells(html)


def test_a_doubted_row_gets_amount_fields_and_correcting_it_clears_the_doubt(env):
    g, ai, store, c, _ = env
    pid = doubtful(store)
    html = c.get(f"/import/{pid}").text
    for f in ("qty", "unit", "packets"):  # the doubted egg row only; flour stays products-only
        assert f'name="{f}_1"' in html and f'name="{f}_0"' not in html
    html = c.post(f"/import/{pid}", data=review_form(action="update", qty_1="1", unit_1="", packets_1="")).text
    assert len(doubt_notes(html)) == 3  # the fields as shown: nothing changed
    html = c.post(f"/import/{pid}", data=review_form(action="update", qty_1="2", unit_1="", packets_1="")).text
    assert doubt_notes(html) == ["No time on the card.", "The card lists salt; no row has it."]
    assert ("cfe-ingredient", "2 dragon egg") in table_cells(html) and amount_inputs(html)["qty_1"] == "2"
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    r = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg, qty_1="2", unit_1="", packets_1=""),
               follow_redirects=False)
    assert r.status_code == 303
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"]) for p in pos] == [(flour, 250), (egg, 2)]


def test_open_doubts_go_onto_the_recipe_until_the_table_editor_saves(env):
    import json
    g, ai, store, c, _ = env
    pid = doubtful(store)
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg, op_label=["mix 2 min", ""]))
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    assert json.loads(g.userfields("recipes", rid)["doubts"]) == [DOUBTS[0], DOUBTS[2]]  # the step's was edited
    html = c.get(f"/recipes/{rid}").text
    assert doubt_notes(html) == ["The card says 2 dragon eggs.", "The card lists salt; no row has it."]
    assert ("cfe-ingredient doubt", "1 dragon egg Check this, note 1") in table_cells(html)
    html = c.get(f"/recipes/{rid}/table").text  # the editor offers the doubted row's amount
    assert doubt_notes(html) == ["The card says 2 dragon eggs.", "The card lists salt; no row has it."]
    assert 'name="qty_1"' in html and 'name="qty_0"' not in html
    t = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    r = c.post(f"/recipes/{rid}/table", data={**chips(t), "action": "save", "qty_1": "2", "unit_1": "",
                                              "packets_1": ""}, follow_redirects=False)
    assert r.status_code == 303 and g.userfields("recipes", rid)["doubts"] == ""
    assert CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients[1].qty == 2
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert pos[1]["amount"] == 2 and pos[1]["note"] == "2 dragon egg"  # the stock amount follows the new qty
    assert doubt_notes(c.get(f"/recipes/{rid}").text) is None


# ---- review round 2: corrections carry through re-renders ------------------------------------------------------
def amount_inputs(html):
    """The amount fields a page shows, as the browser posts them back."""
    return dict(re.findall(r'name="((?:qty|unit|packets)_\d+)" value="([^"]*)"', html))


def test_a_correction_carries_through_update_and_create_then_saves(env):
    g, ai, store, c, _ = env
    pid = doubtful(store)
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    ai.guess = ProductGuess(generic_name="x", is_new_generic=True, stock_unit="g", package_amount=None,
                            location="Pantry", best_before_days=30, g_per_unit=1)
    html = c.post(f"/import/{pid}", data=review_form(action="update", qty_1="2", unit_1="", packets_1="")).text
    assert amount_inputs(html) == {"qty_1": "2", "unit_1": "", "packets_1": ""}  # still there to post again
    assert "The card says 2 dragon eggs." not in doubt_notes(html)  # but its doubt has cleared
    html = c.post(f"/import/{pid}", data=review_form(action="create:0", new_name_0="Rye flour",
                                                     **amount_inputs(html))).text  # Create product re-renders too
    assert amount_inputs(html)["qty_1"] == "2"
    r = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg, **amount_inputs(html)),
               follow_redirects=False)
    assert r.status_code == 303
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    assert CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients[1].qty == 2
    assert "dragon eggs" not in g.userfields("recipes", rid)["doubts"]


def saved_with_doubts(env):
    """The doubtful soup saved as is: all three doubts open on the recipe."""
    g, ai, store, c, _ = env
    pid = doubtful(store)
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg))
    [rid] = [int(x["id"]) for x in g.objects("recipes")]
    return rid, flour, egg


def test_an_editor_correction_survives_add_step(env):
    g, ai, store, c, _ = env
    rid, *_ = saved_with_doubts(env)
    t = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    html = c.post(f"/recipes/{rid}/table", data={**chips(t), "action": "addop", "qty_1": "2", "unit_1": "",
                                                 "packets_1": ""}).text
    assert amount_inputs(html)["qty_1"] == "2"
    assert doubt_notes(html) == ["No time on the card.", "The card lists salt; no row has it."]  # the rest stay
    r = c.post(f"/recipes/{rid}/table", data={**chips(t), "action": "save", **amount_inputs(html)},
               follow_redirects=False)
    assert r.status_code == 303
    assert CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]).ingredients[1].qty == 2


def test_editor_amounts_follow_the_matching_position_not_the_row_order(env):
    from app.ai import Doubt, TableCheck
    g, ai, store, c, _ = env
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    d = soup_draft()
    rid = save_recipe(g, d.model_copy(update={"table": CfeTable(pre=[], ingredients=d.table.ingredients, ops=[])}),
                      [IngredientMatch(0, flour, 250), IngredientMatch(1, egg, 1)], None, None, need_table=False)
    rev = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="", name="dragon egg"),
                                        Ingredient(qty=2, unit="cup", name="flour")],
                   ops=[Op(id="o1", label="mix", inputs=["i0", "i1"])])  # Make the table: rows in another order
    ai.draft = RecipeDraft(title="Soup", servings=4, steps=["Stir"], table=rev)
    ai.check = TableCheck(table=rev, doubts=[Doubt(ref="i0", reason="2 eggs?")])
    assert c.post(f"/recipes/{rid}/make-table", follow_redirects=False).status_code == 303
    t = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    c.post(f"/recipes/{rid}/table", data={**chips(t), "action": "save", "qty_0": "2", "unit_0": "", "packets_0": ""})
    pos = {p["product_id"]: p for p in g.objects("recipes_pos", recipe_id=rid)}
    assert (pos[flour]["amount"], pos[flour]["note"]) == (250, "2 cup flour")  # left alone
    assert (pos[egg]["amount"], pos[egg]["note"]) == (2, "2 dragon egg")


def test_a_packet_typed_as_the_unit_is_refused_on_the_row(env):
    g, ai, store, c, _ = env
    pid = doubtful(store)
    flour, egg = product(g, "Flour"), product(g, "Dragon egg", "piece")
    ai.conversion = ConversionGuess(factor=125, explanation="")
    html = c.post(f"/import/{pid}", data=review_form(product_0=flour, product_1=egg, qty_1="1", unit_1="packet",
                                                     packets_1="")).text
    assert "Use a measure like tbsp or cup; packets go in the packets box." in html and g.objects("recipes") == []
    rid, *_ = saved_with_doubts(env)
    t = CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"])
    html = c.post(f"/recipes/{rid}/table", data={**chips(t), "action": "save", "qty_1": "1", "unit_1": "pouches",
                                                 "packets_1": ""}).text
    assert "Use a measure like tbsp or cup; packets go in the packets box." in html
    assert g.userfields("recipes", rid)["doubts"]  # not saved


@pytest.mark.parametrize("junk", ["{}", "[1, 2]", '[{"ref": 3}]', "not json"])
def test_doubts_that_are_not_a_list_of_doubts_are_none(env, junk):
    g, ai, store, c, _ = env
    rid = brownie_recipe(g)
    g.set_userfields("recipes", rid, {"doubts": junk})
    for url in (f"/recipes/{rid}", f"/recipes/{rid}/table"):
        r = c.get(url)
        assert r.status_code == 200 and doubt_notes(r.text) is None


def test_main_app_sets_up_grocy_before_the_worker_and_starts_without_it(monkeypatch, tmp_path):
    from app import web
    calls = []
    monkeypatch.setattr(web, "load_settings", lambda: Settings("http://grocy.invalid", "k", "", "", None, 6, tmp_path, 3))
    monkeypatch.setattr(web.anthropic, "Anthropic", lambda: None)
    monkeypatch.setattr(web, "bootstrap", lambda grocy: calls.append(("bootstrap", type(grocy).__name__)))
    monkeypatch.setattr(web, "start_worker", lambda *a: calls.append(("worker",)))
    web.main_app()
    assert calls == [("bootstrap", "Grocy"), ("worker",)]  # new userfields exist before anything writes them

    def down(grocy):
        raise GrocyError("Grocy is down")
    monkeypatch.setattr(web, "bootstrap", down)
    calls.clear()
    assert web.main_app() is not None and calls == [("worker",)]  # the app still starts
