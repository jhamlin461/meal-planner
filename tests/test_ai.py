import base64
from types import SimpleNamespace

import pytest

from app.ai import AI, AIError, ProductGuess, RecipeDraft
from app.cfe import CfeTable


def stub(parsed, stop_reason="end_turn"):
    calls = []

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed)

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    return client, calls


DRAFT = RecipeDraft(title="T", servings=None, steps=["a"], table=CfeTable(pre=[], ingredients=[], ops=[]))


def test_structure_recipe_sends_pdf_and_uses_fallbacks():
    client, calls = stub(DRAFT)
    assert AI(client).structure_recipe(text="hi", pdf=b"%PDF-1") is DRAFT
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in kw["betas"]
    assert kw["output_format"] is RecipeDraft
    assert kw["output_config"] == {"effort": "high"}
    block = kw["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert block["source"]["data"] == base64.b64encode(b"%PDF-1").decode()
    assert kw["messages"][0]["content"][-1]["type"] == "text"
    for banned in ("thinking", "temperature", "top_p", "tool_choice"):
        assert banned not in kw


def test_image_media_type_detected():
    client, calls = stub(ProductGuess(generic_name="x", is_new_generic=True, stock_unit="g",
                                      package_amount=None, location="Pantry", best_before_days=1, g_per_unit=1))
    AI(client).guess_product({"name": "x"}, [], [], image=b"\x89PNG\r\n\x1a\nrest")
    assert calls[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    assert calls[0]["output_config"] == {"effort": "low"}


def test_refusal_raises():
    client, _ = stub(None, stop_reason="refusal")
    with pytest.raises(AIError):
        AI(client).guess_conversion("flour", "cup", "g")


def test_sdk_errors_become_aierror():
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    errs = [anthropic.APIConnectionError(request=req),
            anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)]
    for err in errs:
        def parse(**kw):
            raise err
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
        with pytest.raises(AIError):
            AI(client).guess_conversion("flour", "cup", "g")


def test_cfe_prompt_asks_where_each_step_happens():
    from app.ai import CFE_PROMPT
    assert "`where`" in CFE_PROMPT and "same pot" in CFE_PROMPT and "bowl" in CFE_PROMPT


def test_prompt_mentions_packets_and_purpose_notes():
    from app.ai import CFE_PROMPT
    p = CFE_PROMPT
    assert "`packets`" in p and "what we send" in p and "est." in p
    assert "salt" in p and "(for the chicken)" in p and "garlic" in p and "minced" in p  # purpose and prep notes
    assert "Sear 5 min, flip, cook 5 min more; remove" in p and "no trailing period" in p
