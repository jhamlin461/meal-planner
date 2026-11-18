from pathlib import Path

import httpx
import pytest

from app.ai import AIError, IngredientPick, Picks, ProductGuess, RecipeDraft
from app.cfe import CfeTable, Ingredient, Op
from app.config import Settings
from app.importer import (IngredientMatch, PendingStore, add_conversion, create_generic, extract_jsonld_recipe,
                          page_text, process, propose_matches, save_recipe, stock_amount_for)
from app.setup_grocy import bootstrap, ids
from tests.fakes import FakeAI, FakeGrocy

FIX = Path(__file__).parent / "fixtures"


def html(name):
    return (FIX / name).read_text(encoding="utf-8")


def make_draft(ings, servings=4, title="Soup"):
    table = CfeTable(pre=["Heat a pot"], ingredients=[Ingredient(qty=q, unit=u, name=n) for q, u, n in ings],
                     ops=[Op(id="o1", label="simmer", inputs=[f"i{k}" for k in range(len(ings))])])
    return RecipeDraft(title=title, servings=servings, steps=["Chop & stir", "Serve <hot>"], table=table)


def grocy():
    g = FakeGrocy()
    bootstrap(g)
    return g


def product(g, name, unit, **kw):
    return g.create("products", {"name": name, "qu_id_stock": ids(g).unit[unit], **kw})


class RecordingAI(FakeAI):
    def structure_recipe(self, text=None, pdf=None, image=None):
        self.seen = {"text": text, "pdf": pdf}
        return super().structure_recipe(text, pdf, image)

    def match_ingredients(self, names, products):
        self.asked = (names, products)
        return super().match_ingredients(names, products)


def test_extract_jsonld_simple():
    text = extract_jsonld_recipe(html("recipe_jsonld.html"))
    assert "Best Tomato Soup" in text and "4 servings" in text
    assert "2 Tbsp. butter" in text and "Add tomatoes and simmer 20 min." in text
    assert text.index("Melt the butter.") < text.index("Cook the onion")
    assert "grandmother" not in text


def test_extract_jsonld_graph_and_howtosection():
    text = extract_jsonld_recipe(html("recipe_graph.html"))
    assert "Skillet Chicken" in text and "Food Site" not in text
    assert "2 servings" in text and "1 tsp salt" in text
    steps = ["Pat the chicken dry.", "Season with salt.", "Sear skin-side down 8 min.", "Rest 5 min."]
    assert all(s in text for s in steps)
    assert [text.index(s) for s in steps] == sorted(text.index(s) for s in steps)
    assert "Prep" in text and "Cook" in text


def test_extract_jsonld_absent_returns_none():
    page = "<html><head><script>var x = 1;</script><style>p {}</style></head><body><h1>Pie</h1><p>Bake it.</p></body></html>"
    assert extract_jsonld_recipe(page) is None
    text = page_text(page)
    assert "Pie" in text and "Bake it." in text and "var x" not in text and "p {}" not in text


def test_pending_store_roundtrip(tmp_path):
    store = PendingStore(tmp_path / "pending")
    p = store.new("https://example.com/soup")
    q = store.new("soup.pdf", pdf=b"%PDF-1")
    assert store.get(p.id).status == "processing" and store.get(p.id).source == "https://example.com/soup"
    assert store.pdf_path(q.id).read_bytes() == b"%PDF-1" and store.pdf_path(p.id) is None
    assert {x.id for x in store.all()} == {p.id, q.id}
    store.delete(q.id)
    assert store.get(q.id) is None and not (tmp_path / "pending" / f"{q.id}.pdf").exists()
    assert store.get("../../etc/passwd") is None


def test_process_url_marks_ready(tmp_path):
    seen = {}

    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/soup"})
        seen["ua"] = req.headers["user-agent"]
        return httpx.Response(200, text=html("recipe_jsonld.html"))

    store = PendingStore(tmp_path)
    p = store.new("https://example.com/old")
    ai = RecordingAI(draft=make_draft([(1, "can", "tomatoes")]))
    process(store, ai, httpx.Client(transport=httpx.MockTransport(handler)), p.id, "https://example.com/old", None)
    got = store.get(p.id)
    assert got.status == "ready" and got.error is None and got.draft.title == "Soup"
    assert seen["ua"] == "MealPlanner/1.0"
    assert "Best Tomato Soup" in ai.seen["text"] and "grandmother" not in ai.seen["text"]


def test_process_falls_back_to_page_text(tmp_path):
    store = PendingStore(tmp_path)
    p = store.new("https://example.com/pie")
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html><script>x()</script><p>Pie: bake it.</p></html>")))
    ai = RecordingAI(draft=make_draft([(1, "", "egg")]))
    process(store, ai, http, p.id, "https://example.com/pie", None)
    assert store.get(p.id).status == "ready" and "Pie: bake it." in ai.seen["text"] and "x()" not in ai.seen["text"]


def test_process_failure_marks_failed(tmp_path):
    store = PendingStore(tmp_path)
    p = store.new("soup.pdf", pdf=b"%PDF")
    process(store, FakeAI(error=AIError("Claude declined")), httpx.Client(), p.id, None, b"%PDF")
    got = store.get(p.id)
    assert got.status == "failed" and "Claude declined" in got.error and got.draft is None

    q = store.new("https://example.com/gone")
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    process(store, FakeAI(draft=make_draft([(1, "", "egg")])), http, q.id, "https://example.com/gone", None)
    assert store.get(q.id).status == "failed" and "404" in store.get(q.id).error


def test_missing_servings_defaults_to_2(tmp_path):
    store = PendingStore(tmp_path)
    p = store.new("soup.pdf", pdf=b"%PDF-1")
    ai = RecordingAI(draft=make_draft([(1, "", "egg")], servings=None))
    process(store, ai, httpx.Client(), p.id, None, b"%PDF-1")
    assert ai.seen["pdf"] == b"%PDF-1"
    assert store.get(p.id).status == "ready" and store.get(p.id).draft.servings == 2


def test_propose_matches_exact_then_ai():
    g = grocy()
    flour = product(g, "Flour", "g")
    garlic = product(g, "Garlic", "piece")
    product(g, "Great Value Flour", "g", parent_product_id=flour)  # child: not a candidate
    d = make_draft([(1, "cup", "flour"), (2, "cloves", "garlic, minced"), (1, "tbsp.", "unicorn dust")])
    add_conversion(g, flour, "cup", 125)
    picks = Picks(items=[IngredientPick(index=0, product_id=garlic), IngredientPick(index=1, product_id=9999)])
    ai = RecordingAI(picks=picks)
    m = propose_matches(g, ai, d)
    assert m == [IngredientMatch(0, flour, 125), IngredientMatch(1, garlic, 2), IngredientMatch(2, None, None)]
    names, products = ai.asked
    assert names == ["garlic, minced", "unicorn dust"]
    assert {p["id"] for p in products} == {flour, garlic}


def test_propose_matches_ai_error_leaves_unmatched():
    g = grocy()
    product(g, "Flour", "g")
    m = propose_matches(g, FakeAI(error=AIError("x")), make_draft([(1, "", "dragon egg")]))
    assert m == [IngredientMatch(0, None, None)]


def test_save_blocks_unconvertible_unit():
    g = grocy()
    tomatoes = product(g, "Tomatoes", "g")
    d = make_draft([(2, "tbsp", "Tomatoes"), (1, "can", "tomatoes")])
    add_conversion(g, tomatoes, "tbsp", 15)
    m = propose_matches(g, FakeAI(), d)
    assert m[1] == IngredientMatch(1, tomatoes, None)
    with pytest.raises(ValueError, match=r"\b1\b"):
        save_recipe(g, d, m, None, None)
    assert g.objects("recipes") == []
    add_conversion(g, tomatoes, "can", 411)
    m = propose_matches(g, FakeAI(), d)
    assert m == [IngredientMatch(0, tomatoes, 30), IngredientMatch(1, tomatoes, 411)]
    assert save_recipe(g, d, m, None, None) in {r["id"] for r in g.objects("recipes")}


def test_add_conversion_creates_unknown_unit_and_updates_existing():
    g = grocy()
    pid = product(g, "Beans", "g")
    add_conversion(g, pid, "Jar", 300)
    add_conversion(g, pid, "jar", 350)
    jar = ids(g).unit["jar"]
    rows = [c for c in g.objects("quantity_unit_conversions") if c.get("product_id") == pid]
    assert len(rows) == 1 and rows[0]["from_qu_id"] == jar and rows[0]["factor"] == 350
    assert rows[0]["to_qu_id"] == ids(g).unit["g"]


@pytest.mark.parametrize("factor", [0, -1])
def test_add_conversion_rejects_non_positive_factor(factor):
    g = grocy()
    pid = product(g, "Beans", "g")
    before = g.objects("quantity_unit_conversions")
    with pytest.raises(ValueError, match="positive"):
        add_conversion(g, pid, "can", factor)
    assert g.objects("quantity_unit_conversions") == before


@pytest.mark.parametrize("unit", ["packet", "Packets"])
def test_add_conversion_rejects_packaging_unit(unit):
    g = grocy()
    pid = product(g, "Sour cream", "g")
    before = g.objects("quantity_unit_conversions")
    with pytest.raises(ValueError, match="packet|packaging"):
        add_conversion(g, pid, unit, 30)
    assert g.objects("quantity_unit_conversions") == before
    assert "packet" not in ids(g).unit


def test_cfe_prompt_forbids_packets():
    from app.ai import CFE_PROMPT
    assert "packet" in CFE_PROMPT.lower() and "est." in CFE_PROMPT


def test_save_blocks_invalid_table_and_missing_match():
    g = grocy()
    pid = product(g, "Egg", "piece")
    d = make_draft([(1, "", "egg"), (2, "", "egg")])
    with pytest.raises(ValueError, match=r"\b1\b"):
        save_recipe(g, d, [IngredientMatch(0, pid, 1)], None, None)
    d.table.ops[0].inputs = ["i0"]
    with pytest.raises(ValueError, match="i1"):
        save_recipe(g, d, [IngredientMatch(0, pid, 1), IngredientMatch(1, pid, 2)], None, None)


def test_save_writes_positions_and_cfe_userfield(tmp_path):
    g = grocy()
    flour, egg = product(g, "Flour", "g"), product(g, "Egg", "piece")
    g.create("recipes", {"name": "Soup", "base_servings": 2})
    g.create("recipes", {"name": "Soup (2)", "base_servings": 2})
    pending = tmp_path / "pending"
    pending.mkdir()
    pdf = pending / "abc.pdf"
    pdf.write_bytes(b"%PDF-1")
    d = make_draft([(250, "g", "flour"), (2, "large", "egg")], servings=6)
    d.table.ingredients[1].note = "beaten"
    rid = save_recipe(g, d, [IngredientMatch(1, egg, 2), IngredientMatch(0, flour, 250)],
                      "https://example.com/soup", pdf)
    r = g.get_object("recipes", rid)
    assert r["name"] == "Soup (3)" and r["base_servings"] == 6
    assert r["description"] == "<ol><li>Chop &amp; stir</li><li>Serve &lt;hot&gt;</li></ol>"
    pos = sorted(g.objects("recipes_pos", recipe_id=rid), key=lambda p: p["id"])
    assert [(p["product_id"], p["amount"], p["qu_id"]) for p in pos] == [
        (flour, 250, ids(g).unit["g"]), (egg, 2, ids(g).unit["piece"])]
    assert pos[0]["note"] == "250 g flour" and pos[1]["note"] == "2 large egg, beaten"
    uf = g.userfields("recipes", rid)
    assert CfeTable.model_validate_json(uf["cfe_table"]) == d.table
    assert uf["source_url"] == "https://example.com/soup" and uf["source_pdf"] == f"sources/{rid}.pdf"
    assert (tmp_path / "sources" / f"{rid}.pdf").read_bytes() == b"%PDF-1"


def test_create_generic_creates_and_reuses():
    g = grocy()
    settings = Settings("", "", "", "KEY", None, 6, Path("."), 3)
    fdc = {"foods": [{"foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": 20}]}]}
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=fdc)))
    guess = ProductGuess(generic_name="Canned tomatoes", is_new_generic=True, stock_unit="g", package_amount=None,
                         location="Garage", best_before_days=700, g_per_unit=1)
    pid = create_generic(g, FakeAI(guess=guess), http, settings, "Crushed tomatoes")
    p = g.get_object("products", pid)
    assert p["name"] == "Crushed tomatoes" and not p.get("parent_product_id")
    assert p["qu_id_stock"] == ids(g).unit["g"] and p["location_id"] == ids(g).location["Pantry"]
    assert p["default_best_before_days"] == 700
    uf = g.userfields("products", pid)
    assert uf["package_amount"] == 1 and uf["g_per_unit"] == 1 and uf["kcal_100"] == 20
    assert create_generic(g, FakeAI(error=AIError("not called")), http, settings, "crushed TOMATOES") == pid


def test_stock_amount_for_hand_picked_product():
    g = grocy()
    flour = product(g, "Flour", "g")
    add_conversion(g, flour, "cup", 125)
    assert stock_amount_for(g, flour, 2, "cups") == 250
    assert stock_amount_for(g, flour, 2, "tbsp.") is None  # no conversion known
    assert stock_amount_for(g, flour, None, "cup") is None
