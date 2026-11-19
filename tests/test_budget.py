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
    app = create_app(g, FakeAI(picks=Picks(items=[])), httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(404))), settings, PendingStore(tmp_path / "pending"), house=house)
    return TestClient(app, cookies={"who": "kitchen"}), house, g


@pytest.mark.parametrize("dollars,words", [(12.5, "Full"), (83.2, "Economy — cheaper settings until the 1st"),
                                           (100, "Paused until the 1st — using the free fallbacks")])
def test_me_page_shows_this_months_ai_spend_and_mode(web, dollars, words):
    c, house, _ = web
    spend(house, dollars, datetime.now())
    html = c.get("/me").text
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
    return TestClient(app, cookies={"who": "kitchen"}), store


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
