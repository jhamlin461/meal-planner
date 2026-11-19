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
