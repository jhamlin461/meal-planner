"""The single place the app calls Claude (structured outputs only)."""
import base64
import json
from typing import Literal

import anthropic
from pydantic import BaseModel

from app.cfe import CfeTable

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]


class AIError(Exception):
    pass


class RecipeDraft(BaseModel):
    title: str
    servings: int | None
    steps: list[str]
    table: CfeTable


class ProductGuess(BaseModel):
    generic_name: str
    is_new_generic: bool
    stock_unit: Literal["g", "ml", "piece"]
    package_amount: float | None
    location: str
    best_before_days: int
    g_per_unit: float


class ConversionGuess(BaseModel):
    factor: float
    explanation: str


class Suggestion(BaseModel):
    day: str  # ISO date YYYY-MM-DD
    section: Literal["Lunch", "Dinner"]
    recipe_id: int
    reason: str


class WeekSuggestions(BaseModel):
    items: list[Suggestion]


class IngredientPick(BaseModel):
    index: int
    product_id: int | None


class Picks(BaseModel):
    items: list[IngredientPick]


CFE_PROMPT = """You convert a recipe into a Cooking-for-Engineers (CfE) table plus plain steps.
Return title, servings (null if the source does not say), steps (plain numbered steps, text only) and table.
Table rules:
- `ingredients` are listed in the order they are combined; each has qty, unit, name, note.
- `ops` are the actions. An op has id, a short verb label ("melt", "mix", "fold in", "bake 350°F 30–40 min") and inputs.
- Each input is `i<index>` (zero-based ingredient row) or the id of another op.
- Exactly one op is final (the root): nothing consumes it.
- Every ingredient is used by exactly one op.
- An op's inputs must cover contiguous ingredient rows, so order ingredients accordingly.
- An ingredient used in two steps becomes two rows with partial amounts.
- Oven preheat and pan prep go in `pre`, not in ops.
- Never use a packaging unit (packet, pouch, sachet, container, package, bag, box). Convert each packet to a real unit
  (g, ml, tbsp, tsp, cup, oz) using the size printed on the card; if none is printed, use the typical meal-kit packet
  size and put "est." in the note.
- Replace kit-only seasoning blends (e.g. "Southwest Spice Blend") with their component spices: one row each, amounts
  estimated, note "est., from <blend name>". Ops must still cover those rows contiguously."""

PRODUCT_PROMPT = """You fill in pantry product details for a grocery inventory.
Use the given product info (barcode database data or a package photo). Prefer an existing generic name from
`generics` when one fits (is_new_generic=false); otherwise propose a short new one (is_new_generic=true).
location must be one of the given locations. stock_unit is g, ml or piece. package_amount is the package size
in stock_unit, or null if unknown. best_before_days is a typical shelf life once bought. g_per_unit is grams per
stock unit (1 for g, about 1 for ml of water-like liquids, grams per piece for piece)."""

CONVERSION_PROMPT = """Give the multiplier that converts an amount of the named cooking ingredient from
from_unit to to_unit (to = from * factor), with a one-sentence explanation."""

MATCH_PROMPT = """Match each recipe ingredient name (by its index in the list) to the best grocery product.
product_id must be one of the given product ids, or null if nothing reasonably fits. Return one item per ingredient."""

WEEK_PROMPT = """You plan a household's weekly meals from the recipes available.
Suggest Lunch and Dinner items using only recipe_ids from the context, with day as an ISO date (YYYY-MM-DD).
Follow the household rules and constraints in the context (servings, leftovers, expiring stock, recent meals).
Give a short reason for each pick."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _image_block(data: bytes) -> dict:
    mt = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": _b64(data)}}


class AI:
    def __init__(self, client: anthropic.Anthropic):
        self.client = client

    def _parse(self, model_cls, effort: str, system: str, content: list[dict]):
        try:
            resp = self.client.beta.messages.parse(
                model=MODEL, max_tokens=16000, betas=BETAS, fallbacks="default",
                output_format=model_cls, output_config={"effort": effort},
                system=system, messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:  # network, rate limit, auth: callers show AIError, never a 500
            raise AIError(f"Claude unavailable: {e}") from e
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise AIError("Claude declined or returned no result" + (f" ({cat})" if cat else ""))
        return resp.parsed_output

    def structure_recipe(self, text: str | None = None, pdf: bytes | None = None,
                         image: bytes | None = None) -> RecipeDraft:
        content: list[dict] = []
        if pdf:
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": _b64(pdf)}})
        if image:
            content.append(_image_block(image))
        content.append({"type": "text", "text": text or "Structure the attached recipe."})
        return self._parse(RecipeDraft, "high", CFE_PROMPT, content)

    def guess_product(self, info: dict, generics: list[str], locations: list[str],
                      image: bytes | None = None) -> ProductGuess:
        content = [_image_block(image)] if image else []
        content.append({"type": "text", "text": json.dumps(
            {"info": info, "generics": generics, "locations": locations})})
        return self._parse(ProductGuess, "low", PRODUCT_PROMPT, content)

    def guess_conversion(self, product_name: str, from_unit: str, to_unit: str) -> ConversionGuess:
        text = json.dumps({"product": product_name, "from_unit": from_unit, "to_unit": to_unit})
        return self._parse(ConversionGuess, "low", CONVERSION_PROMPT, [{"type": "text", "text": text}])

    def match_ingredients(self, names: list[str], products: list[dict]) -> Picks:
        text = json.dumps({"ingredients": list(enumerate(names)), "products": products})
        return self._parse(Picks, "low", MATCH_PROMPT, [{"type": "text", "text": text}])

    def suggest_week(self, context: dict) -> WeekSuggestions:
        return self._parse(WeekSuggestions, "medium", WEEK_PROMPT,
                           [{"type": "text", "text": json.dumps(context, default=str)}])
