"""Monthly AI budget: the meter, the modes and what each mode sends (Task F). No real API calls: a stub client."""
from datetime import datetime
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ai as ai_mod
from app.ai import AI, Picks, ProductGuess, RecipeDraft, cost, usage_parts
from app.cfe import CfeTable
from app.config import Settings
from app.house import HouseStore
from app.importer import PendingStore
from app.setup_grocy import bootstrap
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy

NOV = datetime(2026, 11, 18, 12)
DRAFT = RecipeDraft(title="T", servings=None, steps=["a"], table=CfeTable(pre=[], ingredients=[], ops=[]))
GUESS = ProductGuess(generic_name="x", is_new_generic=True, stock_unit="g", package_amount=None, location="Pantry",
                     best_before_days=1, g_per_unit=1)


def usage(inp=0, out=0, read=0, w5=0, w1=0, **kw):
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read, cache_creation_input_tokens=w5 + w1,
              cache_creation=NS(ephemeral_5m_input_tokens=w5, ephemeral_1h_input_tokens=w1), **kw)


def client(parsed, model="claude-opus-5-5", u=None):
    """A stub with beta.messages.parse (Opus, fallbacks) and messages.parse (Haiku); records every call."""
    calls = []

    def parse(beta):
        def f(**kw):
            calls.append({**kw, "_beta": beta})
            return NS(stop_reason="end_turn", parsed_output=parsed, model=kw["model"] if not beta else model,
                      usage=u or usage(inp=1_000_000))
        return f
    return NS(beta=NS(messages=NS(parse=parse(True))), messages=NS(parse=parse(False))), calls


def spend(house, dollars, when=NOV):
    house.add_ai_usage(when, "recipe", "claude-opus-5-5", {}, dollars)


# ---- the meter -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("model,field,price", [
    ("claude-opus-5-5", "inp", 4), ("claude-opus-5-5", "out", 20), ("claude-opus-5-5", "read", 0.20),
    ("claude-opus-5-5", "w5", 5), ("claude-opus-5-5", "w1", 8),
    ("claude-sonnet-5-5", "inp", 2), ("claude-sonnet-5-5", "out", 10), ("claude-sonnet-5-5", "read", 0.10),
    ("claude-sonnet-5-5", "w5", 2.5), ("claude-sonnet-5-5", "w1", 4),
    ("claude-haiku-5-5", "inp", 0.10), ("claude-haiku-5-5", "out", 0.50), ("claude-haiku-5-5", "read", 0.01),
    ("claude-haiku-5-5", "w5", 0.125), ("claude-haiku-5-5", "w1", 0.20),
])
def test_cost_of_a_million_tokens_per_field_and_model(model, field, price):
    parts = usage_parts(model, usage(**{field: 1_000_000}))
    assert cost(parts) == pytest.approx(price)
    assert cost(parts, batch=True) == pytest.approx(price / 2)


def test_cost_sums_fallback_iterations_each_at_its_own_model():
    its = [NS(type="message", model="claude-opus-5-5", **vars(usage(inp=1_000_000))),
           NS(type="fallback_message", model="claude-sonnet-5-5", **vars(usage(out=1_000_000)))]
    u = usage(inp=999, out=999, iterations=its)  # the top-level totals are not counted twice
    assert cost(usage_parts("claude-sonnet-5-5", u)) == pytest.approx(4 + 10)


def test_cache_writes_without_a_breakdown_count_as_5_minute():
    u = NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=1_000_000,
           cache_creation=None)
    assert cost(usage_parts("claude-opus-5-5", u)) == pytest.approx(5)


def test_every_call_is_recorded_with_its_cost(tmp_path):
    house = HouseStore(tmp_path / "house.db")
    c, _ = client(GUESS, u=usage(inp=1_000_000, out=100_000))
    AI(c, house).guess_product({"name": "x"}, [], [])
    assert house.ai_spent(ai_mod.month()) == pytest.approx(4 + 2)
    row = house._q("SELECT kind, model, input_tokens, output_tokens, batch FROM ai_usage")[0]
    assert row == ("product", "claude-opus-5-5", 1_000_000, 100_000, 0)


def test_a_call_without_usage_is_not_an_error(tmp_path):
    house = HouseStore(tmp_path / "house.db")
    c = NS(beta=NS(messages=NS(parse=lambda **kw: NS(stop_reason="end_turn", parsed_output=GUESS))))
    assert AI(c, house).guess_product({}, [], []) is GUESS
    assert house.ai_spent(ai_mod.month()) == 0


# ---- modes -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spent,mode", [(0, "full"), (79.99, "full"), (80, "economy"), (99.99, "economy"),
                                        (100, "free"), (140, "free")])
def test_mode_thresholds(tmp_path, spent, mode):
    house = HouseStore(tmp_path / "house.db")
    spend(house, spent)
    assert AI(None, house, budget=100, economy_at=80).mode(NOV) == mode


def test_month_rollover_starts_full_again(tmp_path):
    house = HouseStore(tmp_path / "house.db")
    spend(house, 120)
    ai = AI(None, house)
    assert ai.mode(NOV) == "free" and ai.mode(datetime(2026, 12, 1, 0, 1)) == "full"
    assert house.ai_spent("2026-12") == 0


def test_no_house_is_always_full():
    assert AI(None).mode() == "full"


def calls_per_type(c):
    a = AI(c)
    a.structure_recipe(text="x")
    a.guess_product({"name": "x"}, [], [])
    a.guess_conversion("flour", "cup", "g")
    a.match_ingredients(["flour"], [{"id": 1, "name": "Flour"}])
    a.suggest_week({})
    return a


def test_full_mode_sends_todays_requests_with_a_cached_system_prompt():
    c, calls = client(None)
    c.beta.messages.parse = lambda **kw: calls.append(kw) or NS(stop_reason="end_turn", parsed_output=1)
    calls_per_type(c)
    want = [("high", ai_mod.CFE_PROMPT), ("low", ai_mod.PRODUCT_PROMPT), ("low", ai_mod.CONVERSION_PROMPT),
            ("low", ai_mod.MATCH_PROMPT), ("medium", ai_mod.WEEK_PROMPT)]
    assert len(calls) == 5
    for kw, (effort, prompt) in zip(calls, want):
        assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 16000
        assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
        assert kw["output_config"] == {"effort": effort}
        assert kw["system"] == [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]
        assert set(kw) == {"model", "max_tokens", "betas", "fallbacks", "output_format", "output_config", "system",
                           "messages"}
    assert calls[3]["messages"][0]["content"][0]["text"] == \
        '{"ingredients": [[0, "flour"]], "products": [{"id": 1, "name": "Flour"}]}'


# ---- Me page ---------------------------------------------------------------------------------------------------
@pytest.fixture
def web(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    house = HouseStore(tmp_path / "house.db")
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    ai = FakeAI(picks=Picks(items=[]))
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))), settings, PendingStore(tmp_path / "pending"), house=house)
    c = TestClient(app, cookies={"who": "kitchen"})
    c.app_ai = ai
    return c, house, g


@pytest.mark.parametrize("dollars,words", [(12.5, "Full, the best settings until $80 is used this month"),
                                           (83.2, "Economy — cheaper settings until the 1st"),
                                           (100, "Paused until the 1st — using the free fallbacks")])
def test_me_page_shows_this_months_ai_spend_and_mode(web, dollars, words):
    c, house, _ = web
    spend(house, dollars, datetime.now())
    html = c.get("/settings").text
    assert "AI this month" in html and f"${dollars:.2f} of $100" in html and words in html
    assert "·" not in html


def test_budget_settings_load(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("AI_MONTHLY_BUDGET", "50")
    monkeypatch.delenv("AI_ECONOMY_AT", raising=False)
    s = load_settings()
    assert (s.ai_monthly_budget, s.ai_economy_at) == (50, 80)


# ---- economy ---------------------------------------------------------------------------------------------------
def economy(tmp_path, c):
    house = HouseStore(tmp_path / "house.db")
    spend(house, 85, datetime.now())
    return AI(c, house)


def test_economy_picks_the_cheaper_settings_per_call_type(tmp_path):
    c, calls = client(Picks(items=[]))
    a = economy(tmp_path, c)
    a.structure_recipe(text="x")
    a.guess_product({"name": "x"}, [], [])
    a.guess_conversion("flour", "cup", "g")
    a.match_ingredients(["flour"], [{"id": 1, "name": "Flour"}])
    a.suggest_week({})
    got = [(kw["_beta"], kw["model"], kw["output_config"]["effort"]) for kw in calls]
    assert got == [(True, "claude-opus-5-5", "medium"), (False, "claude-haiku-5-5", "low"),
                   (False, "claude-haiku-5-5", "low"), (False, "claude-haiku-5-5", "low"),
                   (True, "claude-opus-5-5", "medium")]
    for kw in calls:
        assert ("fallbacks" in kw) == kw["_beta"] and ("betas" in kw) == kw["_beta"]
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Haiku calls are metered at Haiku prices: 1M input tokens each
    assert a.spent() == pytest.approx(85 + 4 + 0.1 * 3 + 4)


def test_economy_matching_sends_only_the_30_likeliest_products(tmp_path):
    import json
    c, calls = client(Picks(items=[]))
    products = [{"id": i, "name": f"Thing {i}"} for i in range(100)] + [{"id": 500, "name": "Flour"},
                                                                       {"id": 501, "name": "Butter"}]
    economy(tmp_path, c).match_ingredients(["flour", "unsalted butter"], products)
    sent = json.loads(calls[0]["messages"][0]["content"][0]["text"])["products"]
    assert len(sent) == 30 and {"id": 500, "name": "Flour"} in sent and {"id": 501, "name": "Butter"} in sent
    c2, calls2 = client(Picks(items=[]))
    AI(c2).match_ingredients(["flour"], products)
    assert len(json.loads(calls2[0]["messages"][0]["content"][0]["text"])["products"]) == 102


class Recording(FakeAI):
    def structure_recipe(self, text=None, pdf=None, image=None):
        self.text = text
        return super().structure_recipe(text, pdf, image)


def test_economy_trims_pages_without_jsonld(tmp_path):
    from app.importer import process
    junk = "".join(f"<p>menu item {i}</p>" for i in range(3000))
    page = f"<html>{junk}<h1>Pie</h1><h2>Ingredients</h2><p>1 cup flour</p>{junk}</html>"
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    store = PendingStore(tmp_path / "p")
    texts = {}
    for mode in ("full", "economy"):
        ai = Recording(draft=DRAFT, mode=mode)
        p = store.new("https://example.com/pie")
        process(store, ai, http, p.id, "https://example.com/pie", None)
        texts[mode] = ai.text
    assert len(texts["full"]) > 40_000 and "menu item 0\n" in texts["full"]
    assert len(texts["economy"]) <= 20_000 and "Pie\nIngredients\n1 cup flour" in texts["economy"]
    assert texts["economy"].startswith("menu item 2991\n")  # ten lines of lead-in, not the whole menu


# ---- batch imports -----------------------------------------------------------------------------------------------
def batch_client(results, status="ended"):
    made = []
    batches = NS(create=lambda requests: made.append(requests) or NS(id="msgbatch_1"),
                 retrieve=lambda bid: NS(processing_status=status),
                 results=lambda bid: iter(results))
    return NS(messages=NS(batches=batches)), made


def text_msg(text, u=None):
    return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", model="claude-opus-5-5",
              usage=u or usage(inp=1_000_000))


def test_batch_requests_use_economy_recipe_settings_without_fallbacks(tmp_path):
    c, made = batch_client([])
    bid = economy(tmp_path, c).submit_recipe_batch({"a" * 32: b"%PDF-a", "b" * 32: b"%PDF-b"})
    assert bid == "msgbatch_1"
    reqs = made[0]
    assert [r["custom_id"] for r in reqs] == ["a" * 32, "b" * 32]
    params = reqs[0]["params"]
    assert params["model"] == "claude-opus-5-5" and "fallbacks" not in params and "betas" not in params
    assert params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"][0]["content"][0]["type"] == "document"


def test_batch_results_parse_drafts_and_meter_at_half_price(tmp_path):
    ok = NS(custom_id="a" * 32, result=NS(type="succeeded", message=text_msg(DRAFT.model_dump_json())))
    bad = NS(custom_id="b" * 32, result=NS(type="errored", error=NS()))
    junk = NS(custom_id="c" * 32, result=NS(type="succeeded", message=text_msg("not json")))
    c, _ = batch_client([ok, bad, junk])
    a = economy(tmp_path, c)
    got = a.recipe_batch_results("msgbatch_1")
    assert got["a" * 32] == DRAFT and isinstance(got["b" * 32], str) and isinstance(got["c" * 32], str)
    assert a.spent() == pytest.approx(85 + 2 + 2)
    c2, _ = batch_client([ok], status="in_progress")
    assert economy(tmp_path / "x", c2).recipe_batch_results("msgbatch_1") is None


class BatchAI(FakeAI):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.submitted, self.results = [], None

    def submit_recipe_batch(self, pdfs):
        self.submitted.append(dict(pdfs))
        return "msgbatch_1"

    def recipe_batch_results(self, batch_id):
        assert batch_id == "msgbatch_1"
        return self.results


def import_app(tmp_path, ai):
    g = FakeGrocy()
    bootstrap(g)
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    store = PendingStore(tmp_path / "pending")
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))), settings,
                     store, house=HouseStore(tmp_path / "house.db"))
    c = TestClient(app, cookies={"who": "kitchen"})
    c.grocy = g
    return c, store


def pdfs(n):
    return [("files", (f"r{i}.pdf", b"%PDF-" + bytes([48 + i]), "application/pdf")) for i in range(n)]


def test_three_files_in_economy_go_through_one_batch_and_are_collected_on_the_tick(tmp_path):
    from app.importer import tick
    ai = BatchAI(draft=DRAFT, mode="economy")
    c, store = import_app(tmp_path, ai)
    c.post("/import", files=pdfs(3))
    assert len(ai.submitted) == 1 and len(ai.submitted[0]) == 3
    pend = store.all()
    assert {p.status for p in pend} == {"reading"} and {p.batch_id for p in pend} == {"msgbatch_1"}
    assert "Reading — usually a few minutes" in c.get("/import").text
    assert ai.submitted[0] == {p.id: store.pdf_path(p.id).read_bytes() for p in pend}
    tick(store, ai, None)  # not ended yet
    assert {p.status for p in store.all()} == {"reading"}
    ids_ = [p.id for p in pend]
    ai.results = {ids_[0]: DRAFT, ids_[1]: "Claude couldn't read it (errored)"}
    tick(PendingStore(tmp_path / "pending"), ai, None)  # a restarted app resumes from the stored batch id
    got = {p.id: p for p in store.all()}
    assert got[ids_[0]].status == "ready" and got[ids_[0]].draft.servings == 2 and got[ids_[0]].batch_id is None
    assert got[ids_[1]].status == "failed" and "errored" in got[ids_[1]].error
    assert got[ids_[2]].status == "failed"  # missing from the results


def test_fewer_files_or_full_mode_read_one_at_a_time(tmp_path):
    for mode, n in (("economy", 2), ("full", 3)):
        ai = BatchAI(draft=DRAFT, mode=mode)
        c, store = import_app(tmp_path / mode, ai)
        c.post("/import", files=pdfs(n))
        assert ai.submitted == [] and {p.status for p in store.all()} == {"ready"}


def test_a_failed_batch_submission_reads_them_one_at_a_time(tmp_path):
    from app.ai import AIError

    class Down(BatchAI):
        def submit_recipe_batch(self, pdfs):
            raise AIError("Claude unavailable")
    c, store = import_app(tmp_path, Down(draft=DRAFT, mode="economy"))
    c.post("/import", files=pdfs(3))
    assert {p.status for p in store.all()} == {"ready"}


# ---- free: no Claude calls, each feature's fallback ---------------------------------------------------------------
def test_free_mode_makes_no_claude_call(tmp_path):
    from app.ai import AIPaused

    def boom(**kw):
        raise AssertionError("Claude was called")
    c = NS(beta=NS(messages=NS(parse=boom)), messages=NS(parse=boom, batches=NS(create=boom)))
    house = HouseStore(tmp_path / "house.db")
    spend(house, 100, datetime.now())
    a = AI(c, house)
    for call in (lambda: a.structure_recipe(text="x"), lambda: a.guess_product({}, [], []),
                 lambda: a.guess_conversion("flour", "cup", "g"), lambda: a.match_ingredients(["x"], []),
                 lambda: a.suggest_week({}), lambda: a.submit_recipe_batch({"a" * 32: b"%PDF"})):
        with pytest.raises(AIPaused, match="until the 1st"):
            call()


def jsonld_http():
    from pathlib import Path
    page = (Path(__file__).parent / "fixtures" / "recipe_jsonld.html").read_text(encoding="utf-8")
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=page) if r.url.path == "/soup" else
        httpx.Response(200, text="<p>Pie: bake it.</p>") if r.url.path == "/pie" else httpx.Response(404)))


def test_free_mode_imports_jsonld_without_a_table_and_queues_the_rest(tmp_path):
    from app.importer import process
    store = PendingStore(tmp_path)
    ai = FakeAI(draft=DRAFT, mode="free")
    soup, pie, pdf = store.new("https://example.com/soup"), store.new("https://example.com/pie"), \
        store.new("r.pdf", pdf=b"%PDF-1")
    process(store, ai, jsonld_http(), soup.id, "https://example.com/soup", None)
    process(store, ai, jsonld_http(), pie.id, "https://example.com/pie", None)
    process(store, ai, jsonld_http(), pdf.id, None, b"%PDF-1")
    d = store.get(soup.id).draft
    assert store.get(soup.id).status == "ready" and d.title == "Best Tomato Soup" and d.servings == 4
    assert d.steps == ["Melt the butter.", "Cook the onion until soft.", "Add tomatoes and simmer 20 min."]
    assert d.table.ops == [] and [(i.qty, i.unit, i.name) for i in d.table.ingredients] == [
        (1, "can", "crushed tomatoes"), (2, "tbsp", "butter"), (1, "", "onion, diced")]
    assert store.get(pie.id).status == "waiting" and store.get(pdf.id).status == "waiting"


def test_queued_imports_are_read_after_the_month_rolls_over(tmp_path):
    from app.importer import process, tick
    store = PendingStore(tmp_path)
    ai = FakeAI(draft=DRAFT, mode="free")
    pie, pdf = store.new("https://example.com/pie"), store.new("r.pdf", pdf=b"%PDF-1")
    process(store, ai, jsonld_http(), pie.id, "https://example.com/pie", None)
    process(store, ai, jsonld_http(), pdf.id, None, b"%PDF-1")
    tick(store, ai, jsonld_http())
    assert {p.status for p in store.all()} == {"waiting"}
    ai.mode_ = "full"  # the 1st
    tick(store, ai, jsonld_http())
    assert {p.status for p in store.all()} == {"ready"}


def test_waiting_imports_say_when_they_will_be_read(tmp_path):
    from datetime import date, timedelta
    c, store = import_app(tmp_path, FakeAI(draft=DRAFT, mode="free"))
    c.post("/import", files=pdfs(1))
    [p] = store.all()
    first = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    words = f"Waiting for next month's AI budget — it will be read on {first:%B} 1"
    assert words in c.get("/import").text
    assert words in c.get(f"/import/{p.id}").text


def test_a_tableless_import_saves_ingredients_and_steps(tmp_path):
    from app.setup_grocy import ids
    ai = FakeAI(mode="free")
    c, store = import_app(tmp_path, ai)
    from app.importer import process
    p = store.new("https://example.com/soup")
    process(store, ai, jsonld_http(), p.id, "https://example.com/soup", None)
    html = c.get(f"/import/{p.id}").text
    assert "Add at least one step" not in html and "No table yet" in html
    grocy = c.grocy
    i = ids(grocy)
    pids = [grocy.create("products", {"name": n, "qu_id_stock": i.unit["g"]}) for n in ("Tomatoes", "Butter", "Onion")]
    form = {"action": "save", **{f"product_{k}": str(pid) for k, pid in enumerate(pids)}}  # products only
    r = c.post(f"/import/{p.id}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/recipes"  # no other import waiting
    rid = max(int(x["id"]) for x in grocy.objects("recipes"))
    assert grocy.userfields("recipes", rid)["cfe_table"] == ""
    assert len(grocy.objects("recipes_pos", recipe_id=rid)) == 3
    assert "<li>Melt the butter.</li>" in grocy.get_object("recipes", rid)["description"]
    html = c.get(f"/recipes/{rid}").text  # no conversions in free mode: the recipe's own amounts, never "0 g"
    assert "1 can Tomatoes" in html and "2 tbsp Butter" in html and "1 Onion" in html and "0 g Butter" not in html


def test_free_matching_uses_plain_normalised_names():
    from app.importer import propose_matches
    from app.cfe import Ingredient
    from app.setup_grocy import ids
    g = FakeGrocy()
    bootstrap(g)
    i = ids(g)
    basil, sugar, brown, tomato = (g.create("products", {"name": n, "qu_id_stock": i.unit["g"]})
                                   for n in ("Basil", "Sugar", "Brown sugar", "Tomato"))
    names = ["fresh basil leaves", "packed brown sugar", "ripe tomatoes", "dragon egg"]
    draft = DRAFT.model_copy(update={"table": CfeTable(pre=[], ops=[], ingredients=[
        Ingredient(qty=1, unit="g", name=n) for n in names])})
    got = [m.product_id for m in propose_matches(g, FakeAI(mode="free"), draft)]
    assert got == [basil, brown, tomato, None]


def test_free_scan_asks_for_the_details_and_creates_the_product_like_the_ai_path(tmp_path):
    import json
    from pathlib import Path
    off = json.loads((Path(__file__).parent / "fixtures" / "off_flour.json").read_text())
    g = FakeGrocy()
    bootstrap(g)
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=off) if "openfoodfacts" in r.url.host and "111" in r.url.path
        else httpx.Response(404)))
    app = create_app(g, FakeAI(mode="free"), http, Settings("", "", "", "KEY", None, 6, tmp_path, 3),
                     PendingStore(tmp_path / "pending"), house=HouseStore(tmp_path / "house.db"))
    c = TestClient(app, cookies={"who": "kitchen"})
    assert c.post("/api/scan/lookup", json={"code": "111"}).json() == {
        "status": "manual", "code": "111", "name": "Great Value All Purpose Flour"}
    assert c.post("/api/scan/lookup", json={"code": "222"}).json()["status"] == "manual"  # in no database
    assert 'id="manual"' in c.get("/scan").text
    form = {"code": "111", "name": "Great Value All Purpose Flour", "generic": "Flour",
            "stock_unit": "g", "package_amount": "2268", "location": "Pantry", "category": "food"}
    r = c.post("/api/scan/manual", data=form).json()
    assert r["status"] == "found" and r["created"] and r["generic"] == "Flour" and r["package"] == 2268
    assert r["product_id"] not in g.stock  # the sheet confirms before anything is added
    child = g.get_object("products", r["product_id"])
    parent = g.get_object("products", child["parent_product_id"])
    assert parent["name"] == "Flour" and g.product_by_barcode("111")["product"]["id"] == r["product_id"]
    assert g.userfields("products", r["product_id"])["kcal_100"] == 364  # nutrition from Open Food Facts
    assert c.post("/api/scan/manual", data={**form, "location": "Moon"}).status_code == 400


def test_free_week_suggestions_use_expiring_stock_and_skip_recent_meals(fake_grocy_with_week):
    from datetime import date, timedelta
    from app.suggest import suggest
    g = fake_grocy_with_week
    for r in g.objects("meal_plan"):
        g.delete("meal_plan", r["id"])
    mon = date(2026, 11, 16)
    g.entities["products"][11] = {"id": 11, "name": "Spinach"}
    g.entities["recipes"][2] = {"id": 2, "name": "Spinach pie", "base_servings": 2}
    g.entities["recipes"][3] = {"id": 3, "name": "Stew", "base_servings": 2}
    g.entities["recipes_pos"][2] = {"id": 2, "recipe_id": 2, "product_id": 11, "amount": 1, "note": ""}
    g.add_stock(11, 1, date.today() + timedelta(days=1), None)
    from app.setup_grocy import ids
    g.create("meal_plan", {"day": (mon - timedelta(days=7)).isoformat(), "type": "recipe", "recipe_id": 3,
                           "recipe_servings": 2, "section_id": ids(g).section["Dinner"], "note": "", "done": 0})
    picks = suggest(g, FakeAI(mode="free"), mon)
    assert [(p.day, p.section, p.recipe_id) for p in picks] == [(mon, "Dinner", 2), (mon + timedelta(days=1),
                                                                                       "Dinner", 1)]
    assert "Spinach" in picks[0].reason and picks[1].reason


def test_today_has_a_quiet_note_only_in_free_mode(web):
    c, house, _ = web
    assert "AI is paused" not in c.get("/").text
    spend(house, 100, datetime.now())
    assert "AI is paused until the 1st" in c.get("/").text


def test_free_review_asks_for_nothing_but_the_product(tmp_path):
    from app.cfe import Ingredient, Op
    from app.setup_grocy import ids
    ai = FakeAI(mode="free")
    c, store = import_app(tmp_path, ai)
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour")],
                 ops=[Op(id="o1", label="mix", inputs=["i0"])])
    p = store.new("x")
    p.status, p.draft = "ready", RecipeDraft(title="T", servings=2, steps=[], table=t)
    store.save(p)
    pid = c.grocy.create("products", {"name": "Flour", "qu_id_stock": ids(c.grocy).unit["g"]})
    html = c.post(f"/import/{p.id}", data={"action": "update", "product_0": str(pid)}).text
    assert 'name="product_0"' in html and 'name="factor_0"' not in html and 'value="suggest:0"' not in html


# ---- review round 1 ----------------------------------------------------------------------------------------------
def saved_recipe(g, table=None):
    """A saved recipe with one position (flour) and steps; no table unless given."""
    from app.importer import IngredientMatch, save_recipe
    from app.setup_grocy import ids
    from app.cfe import Ingredient
    pid = g.create("products", {"name": "Flour", "qu_id_stock": ids(g).unit["g"]})
    t = table or CfeTable(pre=[], ops=[], ingredients=[Ingredient(qty=2, unit="cup", name="flour")])
    draft = RecipeDraft(title="Bread", servings=2, steps=["Mix it.", "Bake it."], table=t)
    return save_recipe(g, draft, [IngredientMatch(0, pid, 240)], None, None, need_table=False)


TABLE = CfeTable.model_validate({"pre": [], "ingredients": [{"qty": 2, "unit": "cup", "name": "flour"}],
                                 "ops": [{"id": "o1", "label": "mix", "inputs": ["i0"]}]})


def test_make_the_table_structures_the_saved_recipe(web):
    c, house, g = web
    rid = saved_recipe(g)
    ai = c.app_ai
    ai.draft = DRAFT.model_copy(update={"table": TABLE})
    html = c.get(f"/recipes/{rid}").text
    assert f'action="/recipes/{rid}/make-table"' in html and "Make the table" in html
    r = c.post(f"/recipes/{rid}/make-table", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/recipes/{rid}"
    assert "Bread" in ai.seen_text and "flour" in ai.seen_text.lower() and "Bake it." in ai.seen_text
    assert CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]) == TABLE
    assert "Make the table" not in c.get(f"/recipes/{rid}").text


def test_make_the_table_uses_the_kept_pdf(web, tmp_path):
    c, house, g = web
    rid = saved_recipe(g)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / f"{rid}.pdf").write_bytes(b"%PDF-kept")
    g.set_userfields("recipes", rid, {"source_pdf": f"sources/{rid}.pdf"})
    c.app_ai.draft = DRAFT.model_copy(update={"table": TABLE})
    c.post(f"/recipes/{rid}/make-table")
    assert c.app_ai.seen_pdf == b"%PDF-kept"


def test_make_the_table_waits_for_the_1st_in_free_mode(web):
    c, house, g = web
    rid = saved_recipe(g)
    spend(house, 100, datetime.now())
    c.app_ai.mode_ = "free"
    html = c.get(f"/recipes/{rid}").text
    assert "The table can be made after the 1st" in html and "make-table" not in html
    r = c.post(f"/recipes/{rid}/make-table")
    assert "The table can be made after the 1st" in r.text and g.userfields("recipes", rid)["cfe_table"] == ""


def test_free_review_creates_a_new_product_by_hand_and_saves_end_to_end(tmp_path):
    from app.importer import process
    ai = FakeAI(mode="free")
    c, store = import_app(tmp_path, ai)
    g = c.grocy
    p = store.new("https://example.com/soup")
    process(store, ai, jsonld_http(), p.id, "https://example.com/soup", None)
    html = c.get(f"/import/{p.id}").text
    assert 'name="stock_unit_0"' in html and 'name="location_0"' in html and 'name="category_0"' in html
    form = {"new_name_0": "Crushed tomatoes", "generic_0": "Crushed tomatoes", "stock_unit_0": "g",
            "package_0": "794", "location_0": "Pantry", "category_0": "food", "action": "create:0"}
    html = c.post(f"/import/{p.id}", data=form).text
    tom = next(x for x in g.objects("products") if x["name"] == "Crushed tomatoes")
    assert not tom.get("parent_product_id") and g.userfields("products", tom["id"])["package_amount"] == 794
    assert f'<option value="{tom["id"]}" selected>' in html
    from app.setup_grocy import ids
    i = ids(g)
    butter = g.create("products", {"name": "Butter", "qu_id_stock": i.unit["g"]})
    onion = g.create("products", {"name": "Onion", "qu_id_stock": i.unit["piece"]})
    save = {"action": "save", "product_0": tom["id"], "product_1": butter, "product_2": onion}
    r = c.post(f"/import/{p.id}", data=save, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/recipes"
    rid = max(int(x["id"]) for x in g.objects("recipes"))
    assert [x["product_id"] for x in g.objects("recipes_pos", recipe_id=rid)] == [tom["id"], butter, onion]
    assert g.userfields("recipes", rid)["cfe_table"] == ""


def test_full_mode_still_needs_a_step(tmp_path):
    c, store = import_app(tmp_path, FakeAI())
    p = store.new("x")
    p.status = "ready"
    p.draft = RecipeDraft(title="T", servings=2, steps=[], table=CfeTable(pre=[], ops=[], ingredients=[]))
    store.save(p)
    html = c.post(f"/import/{p.id}", data={"action": "save"}).text
    assert "Add at least one step" in html and not c.grocy.objects("recipes")


def batch_house(tmp_path):
    """An economy AI with a real meter and a stub batches API whose results are set later."""
    house = HouseStore(tmp_path / "house.db")
    spend(house, 85, datetime.now())
    results = []
    batches = NS(create=lambda requests: NS(id="msgbatch_1"), retrieve=lambda bid: NS(processing_status="ended"),
                 results=lambda bid: iter(results))
    from app.ai import TableCheck
    check = NS(stop_reason="end_turn", parsed_output=TableCheck(table=DRAFT.table, doubts=[]), usage=usage())
    beta = NS(messages=NS(parse=lambda **kw: check))  # the accuracy check on each collected read: no doubts, $0
    return AI(NS(messages=NS(batches=batches), beta=beta), house), house, results


def submitted(tmp_path, ai, results):
    from app.importer import submit_batch
    store = PendingStore(tmp_path / "pending")
    pend = [store.new(f"r{n}.pdf", pdf=b"%PDF-" + bytes([48 + n])) for n in range(3)]
    assert submit_batch(store, ai, pend)
    results += [NS(custom_id=p.id, result=NS(type="succeeded", message=text_msg(DRAFT.model_dump_json())))
                for p in pend]
    return store, pend


def test_a_batch_is_metered_once_even_when_collecting_fails_midway(tmp_path, monkeypatch):
    from app import importer
    ai, house, results = batch_house(tmp_path)
    store, pend = submitted(tmp_path, ai, results)
    calls = []

    def flaky(pdf):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk hiccup")
        return None
    monkeypatch.setattr(importer, "pdf_photo", flaky)
    importer.tick(store, ai, None)
    assert {p.status for p in store.all()} == {"ready", "reading"}
    importer.tick(store, ai, None)
    assert {p.status for p in store.all()} == {"ready"}
    assert ai.spent() == pytest.approx(85 + 3 * 2)  # 1M input tokens each at half of $4, counted once
    assert house.open_batches() == []


def test_a_batch_whose_imports_were_all_discarded_is_still_metered(tmp_path):
    from app.importer import tick
    ai, house, results = batch_house(tmp_path)
    store, pend = submitted(tmp_path, ai, results)
    for p in pend:
        store.delete(p.id)
    assert house.open_batches() == ["msgbatch_1"]
    tick(store, ai, None)
    tick(store, ai, None)
    assert ai.spent() == pytest.approx(85 + 6) and house.open_batches() == []


def test_one_bad_batch_does_not_stall_the_others(tmp_path):
    from app.importer import tick
    store = PendingStore(tmp_path)
    a, b = store.new("a.pdf", pdf=b"%PDF-a"), store.new("b.pdf", pdf=b"%PDF-b")
    for p, bid in ((a, "bad"), (b, "good")):
        p.status, p.batch_id = "reading", bid
        store.save(p)

    class TwoBatches(FakeAI):
        def recipe_batch_results(self, bid):
            if bid == "bad":
                raise RuntimeError("broken")
            return {b.id: DRAFT}
    tick(store, TwoBatches(), None)
    assert store.get(b.id).status == "ready" and store.get(a.id).status == "reading"


def test_manual_product_needs_a_positive_package_size(tmp_path):
    g = FakeGrocy()
    bootstrap(g)
    app = create_app(g, FakeAI(mode="free"), httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))), Settings("", "", "", "KEY", None, 6, tmp_path, 3),
        PendingStore(tmp_path / "pending"), house=HouseStore(tmp_path / "house.db"))
    c = TestClient(app, cookies={"who": "kitchen"})
    form = {"code": "111", "name": "Rice", "generic": "Rice", "stock_unit": "g", "location": "Pantry",
            "category": "food"}
    for bad in ("0", "-3", "lots"):
        assert c.post("/api/scan/manual", data={**form, "package_amount": bad}).status_code == 400
    assert c.post("/api/scan/manual", data={**form, "package_amount": ""}).json()["package"] is None
    assert 'id="manual-cancel"' in c.get("/scan").text


def test_import_page_refreshes_slowly_while_only_batches_read(tmp_path):
    c, store = import_app(tmp_path, FakeAI())
    p = store.new("a.pdf", pdf=b"%PDF")
    p.status, p.batch_id = "reading", "msgbatch_1"
    store.save(p)
    assert 'http-equiv="refresh" content="30"' in c.get("/import").text
    store.new("b.pdf", pdf=b"%PDF")  # processing
    assert 'http-equiv="refresh" content="5"' in c.get("/import").text


def test_free_week_suggestions_count_brand_stock_toward_its_generic(fake_grocy_with_week):
    from datetime import date, timedelta
    from app.suggest import suggest
    g = fake_grocy_with_week
    for r in g.objects("meal_plan"):
        g.delete("meal_plan", r["id"])
    mon = date(2026, 11, 16)
    g.entities["products"][11] = {"id": 11, "name": "Spinach"}
    g.entities["products"][12] = {"id": 12, "name": "Acme baby spinach", "parent_product_id": 11}
    g.entities["recipes"][2] = {"id": 2, "name": "Spinach pie", "base_servings": 2}
    g.entities["recipes_pos"][2] = {"id": 2, "recipe_id": 2, "product_id": 11, "amount": 1, "note": ""}
    g.add_stock(12, 1, date.today() + timedelta(days=1), None)
    picks = suggest(g, FakeAI(mode="free"), mon)
    assert picks[0].recipe_id == 2 and "Spinach" in picks[0].reason


def table_app(tmp_path, ai, page=None):
    """An app whose web fetches are recorded; `page` is served for every URL (None: the site is down)."""
    g = FakeGrocy()
    bootstrap(g)
    fetched = []

    def handler(r):
        fetched.append(str(r.url))
        return httpx.Response(200, text=page) if page else httpx.Response(503)
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(handler)),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"),
                     house=HouseStore(tmp_path / "house.db"))
    rid = saved_recipe(g)
    g.set_userfields("recipes", rid, {"source_url": "https://example.com/bread"})
    return TestClient(app, cookies={"who": "kitchen"}), g, rid, fetched


def test_make_the_table_in_free_mode_fetches_nothing(tmp_path):
    c, g, rid, fetched = table_app(tmp_path, FakeAI(mode="free"), page="<p>Bread</p>")
    r = c.post(f"/recipes/{rid}/make-table")
    assert "The table can be made after the 1st" in r.text and fetched == []
    assert g.userfields("recipes", rid)["cfe_table"] == ""


def test_make_the_table_says_when_it_used_the_saved_text(tmp_path):
    ai = FakeAI(draft=DRAFT.model_copy(update={"table": TABLE}))
    c, g, rid, fetched = table_app(tmp_path, ai)
    r = c.post(f"/recipes/{rid}/make-table")
    assert fetched and "Bake it." in ai.seen_text and g.userfields("recipes", rid)["cfe_table"]
    from html import unescape
    assert "Made from the saved recipe text — the original page couldn't be reached." in unescape(r.text)
    c2, g2, rid2, _ = table_app(tmp_path / "up", ai, page="<p>Bread from the page</p>")
    r2 = c2.post(f"/recipes/{rid2}/make-table")
    assert "Bread from the page" in ai.seen_text and "saved recipe text" not in r2.text


# ---- the accuracy check (Task 3) ---------------------------------------------------------------------------------
def test_the_check_uses_the_recipe_settings_and_sends_the_source_and_table(tmp_path):
    import json
    from app.ai import TableCheck
    answer = TableCheck(table=TABLE, doubts=[])
    c, calls = client(answer)
    assert AI(c).check_table(TABLE, 2, pdf=b"%PDF-x") == answer
    c2, calls2 = client(answer)
    economy(tmp_path, c2).check_table(TABLE, None, text="2 cups flour. Mix.")
    assert ai_mod.ECONOMY["check"] == ai_mod.ECONOMY["recipe"]
    got = [(kw["_beta"], kw["model"], kw["output_config"]["effort"]) for kw in calls + calls2]
    assert got == [(True, "claude-opus-5-5", "high"), (True, "claude-opus-5-5", "medium")]
    for kw in calls + calls2:
        assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["output_format"] is TableCheck
    full, eco = calls[0]["messages"][0]["content"], calls2[0]["messages"][0]["content"]
    assert full[0]["type"] == "document" and eco[0]["text"] == "2 cups flour. Mix."
    sent = json.loads(full[-1]["text"])
    assert sent == {"servings": 2, "table": TABLE.model_dump()}


def test_batch_and_make_table_run_check(tmp_path, web):
    from app.ai import Doubt, TableCheck
    from app.importer import tick
    fixed = TABLE.model_copy(update={"ops": [TABLE.ops[0].model_copy(update={"label": "Mix 2 min"})]})
    check = TableCheck(table=fixed, doubts=[Doubt(ref="o1", reason="The card gives no time.")])
    # a batch read is checked by a normal call once its result arrives, against its own PDF
    ai = BatchAI(draft=DRAFT.model_copy(update={"table": TABLE}), mode="economy", check=check)
    ic, store = import_app(tmp_path / "batch", ai)
    ic.post("/import", files=pdfs(3))
    ai.results = {p.id: ai.draft for p in store.all()}
    tick(store, ai, None)
    for p in store.all():
        assert p.status == "ready" and p.draft.table == fixed
        assert p.doubts == [{"ref": "o1", "reason": "The card gives no time."}]
    assert ai.checked["pdf"] in {store.pdf_path(p.id).read_bytes() for p in store.all()}

    # Make the table: the corrections are saved and the doubts shown with the new table
    c, house, g = web
    rid = saved_recipe(g)
    c.app_ai.draft, c.app_ai.check = DRAFT.model_copy(update={"table": TABLE}), check
    r = c.post(f"/recipes/{rid}/make-table", follow_redirects=False)
    assert r.status_code == 200 and CfeTable.model_validate_json(g.userfields("recipes", rid)["cfe_table"]) == fixed
    assert 'role="note"' in r.text and "The card gives no time." in r.text and 'class="cfe-op doubt"' in r.text
    assert "Bake it." in c.app_ai.checked["text"]
