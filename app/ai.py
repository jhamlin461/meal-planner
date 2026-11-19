"""The single place the app calls Claude (structured outputs only)."""
import base64
import json
import logging
from datetime import datetime
from difflib import SequenceMatcher
from typing import Literal

import anthropic
from anthropic import transform_schema  # the SDK's own pydantic -> strict JSON schema
from pydantic import BaseModel

from app.cfe import CfeTable
from app.setup_grocy import HOME_LOCATIONS

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
HAIKU = "claude-haiku-5-5"  # no server-side fallbacks: called without betas/fallbacks
# Economy mode, per call type: (model, effort). Week suggestions are not listed: they stay as in full mode.
ECONOMY = {"recipe": (MODEL, "medium"), "check": (MODEL, "medium"), "product": (HAIKU, "low"),
           "conversion": (HAIKU, "low"), "match": (HAIKU, "low"), "measure": (HAIKU, "low"),
           "walmart": (HAIKU, "low")}
MATCH_LIMIT = 30  # economy: products sent with an ingredient list
log = logging.getLogger(__name__)

# List prices in USD per million tokens, from the Pricing page fetched 2026-11-18 (Haiku: prompts up to 100k):
# (input, output, cache read, 5-minute cache write, 1-hour cache write). The Batch API is half of every line.
PRICES = {
    "opus": (4, 20, 0.20, 5, 8),          # Opus 5.5
    "sonnet": (2, 10, 0.10, 2.5, 4),      # Sonnet 5.5
    "haiku": (0.10, 0.50, 0.01, 0.125, 0.20),  # Haiku 5.5
}
TOKENS = ("input_tokens", "output_tokens", "cache_read", "cache_write_5m", "cache_write_1h")  # PRICES' order


def _tokens(u) -> dict:
    cc = getattr(u, "cache_creation", None)
    w5 = (getattr(cc, "ephemeral_5m_input_tokens", 0) or 0) if cc else 0
    w1 = (getattr(cc, "ephemeral_1h_input_tokens", 0) or 0) if cc else 0
    if not (w5 or w1):  # no breakdown: count the writes at the 5-minute price (the default TTL)
        w5 = getattr(u, "cache_creation_input_tokens", 0) or 0
    return dict(zip(TOKENS, (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                             getattr(u, "cache_read_input_tokens", 0) or 0, w5, w1)))


def usage_parts(model: str, usage) -> list[tuple[str, dict]]:
    """(model, token counts) per model that worked on a response: each server-side fallback iteration on its own."""
    its = getattr(usage, "iterations", None)
    return [(getattr(i, "model", None) or model, _tokens(i)) for i in its] if its else [(model, _tokens(usage))]


def cost(parts: list[tuple[str, dict]], batch: bool = False) -> float:
    total = 0.0
    for model, t in parts:
        price = next((v for k, v in PRICES.items() if k in model), PRICES["opus"])  # unknown: the dearest
        total += sum(t[k] * p for k, p in zip(TOKENS, price)) / 1e6
    return total / 2 if batch else total


def month(now: datetime | None = None) -> str:
    """The budget month, 'YYYY-MM' in local time."""
    return (now or datetime.now()).strftime("%Y-%m")


def budget_mode(spent: float, budget: float, economy_at: float) -> Literal["full", "economy", "free"]:
    return "free" if spent >= budget else "economy" if spent >= economy_at else "full"


class AIError(Exception):
    pass


class AIPaused(AIError):
    """Free mode: this month's budget is used, so nothing was sent to Claude; callers use their fallback."""

    def __init__(self):
        super().__init__("AI is paused until the 1st (this month's budget is used)")


class RecipeDraft(BaseModel):
    title: str
    servings: int | None
    steps: list[str]
    table: CfeTable


class Doubt(BaseModel):
    ref: str  # "i<index>" (an ingredient row), an op id, or "" for the table as a whole (something missing)
    reason: str


class TableCheck(BaseModel):
    table: CfeTable
    doubts: list[Doubt]


class ProductGuess(BaseModel):
    generic_name: str
    is_new_generic: bool
    stock_unit: Literal["g", "ml", "piece"]
    package_amount: float | None
    location: str
    best_before_days: int
    g_per_unit: float
    category: Literal["food", "supply"] = "food"
    measure: str = ""  # the household measure it's counted in ("cups", "rolls"); see MEASURE_RULES
    measure_factor: float | None = None  # stock units per 1 measure


class MeasureGuess(BaseModel):
    measure: str
    factor: float


class WalmartRead(BaseModel):
    package_amount: float | None = None  # what the listing sells, in the product's stock unit
    grams_per_piece: float | None = None
    measure_factor: float | None = None  # stock units per 1 of the product's household measure


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
- `ingredients` are listed in the order they are combined; each has qty, unit, name, note, packets.
- `ops` are the actions. An op has id, a short label and inputs.
- Each input is `i<index>` (zero-based ingredient row) or the id of another op.
- Exactly one op is final (the root): nothing consumes it.
- Every ingredient is used by exactly one op.
- An op's inputs must cover contiguous ingredient rows, so order ingredients accordingly.
- An ingredient used in two steps becomes two rows with partial amounts.
- Oven preheat and pan prep go in `pre`, not in ops.
- Each op's `where` names the vessel, short: "in a large pot", "in a small bowl", "on a sheet pan". Use the one the
  recipe names, else a sensible home-kitchen choice; never blank for an op that combines things. An op that carries
  on in its input's vessel says so ("same pot").
- `name` is the plain ingredient ("garlic", "grape tomatoes"), nothing else in it.
- `note` holds prep words, then the row's purpose in parentheses: "minced" ("5 cloves garlic, minced"), "halved (raw,
  added later)", "(for the chicken)", "(for searing)". Give the purpose whenever an ingredient is split over rows.
- Op labels are short imperatives with times and temperatures inline as the card writes them, semicolons between
  actions, no trailing period: "Sear 5 min, flip, cook 5 min more; remove", "Cook tomatoes, garlic & spices 2 min",
  "Roast 425°F 18–20 min", "Melt", "Fold in".
- Never use a packaging unit (packet, pouch, sachet, container, package, bag, box) as `unit`. qty and unit are always
  the measured amount (g, ml, tbsp, tsp, cup, oz, clove...): the size printed on the card, else the typical meal-kit
  packet size with "est." in the note's parentheses ("(est.)", "minced (est.)").
- `packets`: meal kits (EveryPlate, HelloFresh) list "what we send" per box size, e.g. "Umami Ginger Sauce (1 | 2)" for
  2 | 4 servings. Set `packets` to the count in the column matching the recipe's servings (here 1 for 2 servings) and
  keep the measured amount in qty and unit. Staples the card says to have at home (salt, pepper, oil, butter) and
  every non-kit recipe get null.
- Replace kit-only seasoning blends (e.g. "Southwest Spice Blend") with their component spices: one row each, amounts
  estimated, note "(est., from <blend name>)", packets null. Ops must still cover those rows contiguously."""

CHECK_PROMPT = """You check a Cooking-for-Engineers table against the recipe it was made from; someone will cook from
the table alone, so it must match the card exactly. The user message gives the recipe, then the table and the servings
it is for (null: the card's own).
Compare, against the recipe: every amount and unit, every packet count (the column for those servings), every
ingredient note, every temperature and time in the step labels and in each step's `where` (an oven temperature may
sit there), and which ingredients each step combines. Correct anything wrong in place.
Return the corrected table with the same ingredient rows and the same steps (same ids) in the same order: never add,
remove or reorder a row or a step, and keep the table valid under the rules below.
Add a doubt for anything you are not sure of, anything the recipe has that the table lacks, and anything in the
table the recipe doesn't have. A doubt's ref is the ingredient row ("i<index>", zero-based) or the step's id it is
about, or "" when it belongs to no row or step (something missing). Its reason is one short sentence for the cook,
naming the ingredient or step. No doubts when everything matches.

The table was made with these instructions:
""" + CFE_PROMPT

MEASURE_RULES = """The measure is what the household counts or measures it in, in the plural: a container or count
word for supplies, packaged goods and things counted one by one ("rolls", "bags", "cans", "bottles", "eggs"), or a
kitchen measure for loose food (cups, tbsp, tsp, fl oz, oz, lb), never g or ml. Its factor is how many stock units one
of it is (a cup of flour in g: about 125; a roll of paper towels in piece: 1; a bottle of dish soap in ml: its size)."""

PRODUCT_PROMPT = """You fill in pantry product details for a grocery inventory.
Use the given product info (barcode database data or a package photo). Prefer an existing generic from
`generics` when one fits (is_new_generic=false, generic_name = its name exactly); otherwise propose a short new
name (is_new_generic=true). location must be one of the given locations. stock_unit is g, ml or piece; when you
pick an existing generic, stock_unit must be that generic's stock_unit. package_amount is the package size in
stock_unit (e.g. count the tortillas when the generic is in piece), or null if unknown or not expressible in it. best_before_days is a typical shelf life once bought. g_per_unit is grams per
stock unit (1 for g, about 1 for ml of water-like liquids, grams per piece for piece).
category is supply for non-food household consumables (trash bags, toilet paper, detergent, cat litter,
cleaning products), else food. Supplies never expire (best_before_days -1) and go where they are kept at home
(""" + ", ".join(HOME_LOCATIONS) + """).
measure and measure_factor: """ + MEASURE_RULES

CONVERSION_PROMPT = """Give the multiplier that converts an amount of the named cooking ingredient from
from_unit to to_unit (to = from * factor), with a one-sentence explanation."""

MEASURE_PROMPT = """Pick the household measure for a pantry or household product stocked in the given stock_unit.
""" + MEASURE_RULES + """ Return measure and factor."""

WALMART_PROMPT = """A household product is linked to a Walmart listing. From the listing words (URL slug and page title),
give what is plainly stated and null for the rest: package_amount, the size of one package in the given stock_unit
(convert oz, lb, fl oz, count to g, ml or piece); grams_per_piece, for a product counted in pieces; measure_factor,
stock units in one cup (or the product's household measure) only when the listing makes it obvious."""

MATCH_PROMPT = """Match each recipe ingredient name (by its index in the list) to the best grocery product.
product_id must be one of the given product ids, or null if nothing reasonably fits. Return one item per ingredient."""

WEEK_PROMPT = """You plan a household's weekly meals from the recipes available.
Suggest Lunch and Dinner items using only recipe_ids from the context, with day as an ISO date (YYYY-MM-DD).
Follow the household rules and constraints in the context (servings, leftovers, expiring stock, recent meals).
Give a short reason for each pick."""

STOCK_WEEK_PROMPT = WEEK_PROMPT + """
Plan from what the household already has: fill the slots from fully stocked recipes first (a recipe with kit true has
its meal kit in the house: use it first), then the most stocked (fulfillment), then recipes that use expiring stock.
Never repeat a recipe in the week. Avoid recipes cooked in the last 3 weeks (recent_recipes) unless nothing else is
fully stocked."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _image_block(data: bytes) -> dict:
    mt = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": _b64(data)}}


def _system(text: str) -> list[dict]:
    """The system prompt, cached: repeated instructions and schema are read at a tenth of the price."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _recipe_content(text: str | None = None, pdf: bytes | None = None, image: bytes | None = None,
                    ask: str = "Structure the attached recipe.") -> list[dict]:
    content: list[dict] = []
    if pdf:
        content.append({"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf", "data": _b64(pdf)}})
    if image:
        content.append(_image_block(image))
    content.append({"type": "text", "text": text or ask})
    return content


def shortlist(names: list[str], products: list[dict], n: int = MATCH_LIMIT) -> list[dict]:
    """The n products whose names are most like any ingredient name (a local fuzzy match, no Claude).
    ponytail: O(names x products) SequenceMatcher; fine for a household's few hundred generics."""
    low = [x.lower() for x in names]

    def score(p: dict) -> float:
        a = p["name"].lower()
        return max((SequenceMatcher(None, a, b).ratio() + bool(set(a.split()) & set(b.split())) for b in low),
                   default=0)
    return sorted(products, key=score, reverse=True)[:n]


class AI:
    """house: a HouseStore that meters every call (None: no meter, always full mode)."""

    def __init__(self, client: anthropic.Anthropic, house=None, budget: float = 100, economy_at: float = 80):
        self.client, self.house, self.budget, self.economy_at = client, house, budget, economy_at

    def spent(self, now: datetime | None = None) -> float:
        return self.house.ai_spent(month(now)) if self.house else 0.0

    def mode(self, now: datetime | None = None) -> Literal["full", "economy", "free"]:
        return budget_mode(self.spent(now), self.budget, self.economy_at)

    @staticmethod
    def _usage(kind: str, resp, batch: bool = False) -> tuple[str, str, dict, float]:
        """(kind, model, summed tokens, cost) for a response."""
        model = getattr(resp, "model", None) or MODEL
        parts = usage_parts(model, resp.usage)
        return kind, model, {k: sum(t[k] for _, t in parts) for k in TOKENS}, cost(parts, batch)

    def _record(self, kind: str, resp) -> None:
        """Meter a response; metering never fails a call."""
        if self.house is None or getattr(resp, "usage", None) is None:
            return
        try:
            self.house.add_ai_usage(datetime.now(), *self._usage(kind, resp))
        except Exception:
            log.warning("could not record AI usage", exc_info=True)

    def open_batches(self) -> list[str]:
        """Submitted batches not yet metered (collected even when their imports were discarded)."""
        return self.house.open_batches() if self.house else []

    def _parse(self, kind: str, model_cls, effort: str, system: str, content: list[dict]):
        model, mode = MODEL, self.mode()
        if mode == "free":
            raise AIPaused()
        if mode == "economy":
            model, effort = ECONOMY.get(kind, (model, effort))
        kw = dict(model=model, max_tokens=16000, output_format=model_cls, output_config={"effort": effort},
                  system=_system(system), messages=[{"role": "user", "content": content}])
        try:
            if model == MODEL:
                resp = self.client.beta.messages.parse(**kw, betas=BETAS, fallbacks="default")
            else:
                resp = self.client.messages.parse(**kw)
        except anthropic.APIError as e:  # network, rate limit, auth: callers show AIError, never a 500
            raise AIError(f"Claude unavailable: {e}") from e
        self._record(kind, resp)
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise AIError("Claude declined or returned no result" + (f" ({cat})" if cat else ""))
        return resp.parsed_output

    def structure_recipe(self, text: str | None = None, pdf: bytes | None = None,
                         image: bytes | None = None) -> RecipeDraft:
        return self._parse("recipe", RecipeDraft, "high", CFE_PROMPT, _recipe_content(text, pdf, image))

    def check_table(self, table: CfeTable, servings: int | None, text: str | None = None, pdf: bytes | None = None,
                    image: bytes | None = None) -> TableCheck:
        """The accuracy check: Claude re-reads the source against the table, correcting it and saying what it doubts."""
        content = _recipe_content(text, pdf, image, ask="The recipe is attached.")
        content.append({"type": "text", "text": json.dumps({"servings": servings, "table": table.model_dump()})})
        return self._parse("check", TableCheck, "high", CHECK_PROMPT, content)

    def submit_recipe_batch(self, pdfs: dict[str, bytes]) -> str:
        """Economy: read several PDFs through the Message Batches API at half price (results usually in minutes,
        at most a day). Keys become custom_ids (pending ids). Returns the batch id."""
        if self.mode() == "free":
            raise AIPaused()
        model, effort = ECONOMY["recipe"]
        fmt = {"type": "json_schema", "schema": transform_schema(RecipeDraft)}
        reqs = [{"custom_id": cid, "params": {
            "model": model, "max_tokens": 16000, "output_config": {"effort": effort, "format": fmt},
            "system": _system(CFE_PROMPT), "messages": [{"role": "user", "content": _recipe_content(pdf=pdf)}]}}
            for cid, pdf in pdfs.items()]
        try:
            bid = self.client.messages.batches.create(requests=reqs).id
        except anthropic.APIError as e:
            raise AIError(f"Claude unavailable: {e}") from e
        if self.house:
            self.house.add_batch(bid)
        return bid

    def recipe_batch_results(self, batch_id: str) -> dict[str, RecipeDraft | str] | None:
        """None while the batch runs; then each custom_id's draft, or why there is none. The batch's usage is
        metered the first time its results are read, never again."""
        try:
            if self.client.messages.batches.retrieve(batch_id).processing_status != "ended":
                return None
            out: dict[str, RecipeDraft | str] = {}
            usage = []
            for r in self.client.messages.batches.results(batch_id):
                if r.result.type != "succeeded":
                    out[r.custom_id] = f"Claude couldn't read it ({r.result.type})"
                    continue
                msg = r.result.message
                if getattr(msg, "usage", None) is not None:
                    usage.append(self._usage("recipe", msg, batch=True))
                try:
                    text = next(b.text for b in msg.content if b.type == "text")
                    out[r.custom_id] = RecipeDraft.model_validate_json(text)
                except (StopIteration, ValueError):  # a refusal, or JSON that isn't a draft
                    out[r.custom_id] = "Claude declined or returned no result"
        except anthropic.APIError as e:
            raise AIError(f"Claude unavailable: {e}") from e
        if self.house:
            self.house.meter_batch(batch_id, datetime.now(), usage)
        return out

    def guess_product(self, info: dict, generics: list[dict], locations: list[str],
                      image: bytes | None = None) -> ProductGuess:
        content = [_image_block(image)] if image else []
        content.append({"type": "text", "text": json.dumps(
            {"info": info, "generics": generics, "locations": locations})})
        return self._parse("product", ProductGuess, "low", PRODUCT_PROMPT, content)

    def guess_conversion(self, product_name: str, from_unit: str, to_unit: str) -> ConversionGuess:
        text = json.dumps({"product": product_name, "from_unit": from_unit, "to_unit": to_unit})
        return self._parse("conversion", ConversionGuess, "low", CONVERSION_PROMPT, [{"type": "text", "text": text}])

    def guess_measure(self, name: str, stock_unit: str) -> MeasureGuess:
        text = json.dumps({"product": name, "stock_unit": stock_unit})
        return self._parse("measure", MeasureGuess, "low", MEASURE_PROMPT, [{"type": "text", "text": text}])

    def read_walmart(self, name: str, stock_unit: str, text: str) -> WalmartRead:
        text = json.dumps({"product": name, "stock_unit": stock_unit, "listing": text})
        return self._parse("walmart", WalmartRead, "low", WALMART_PROMPT, [{"type": "text", "text": text}])

    def match_ingredients(self, names: list[str], products: list[dict]) -> Picks:
        if len(products) > MATCH_LIMIT and self.mode() == "economy":
            products = shortlist(names, products)
        text = json.dumps({"ingredients": list(enumerate(names)), "products": products})
        return self._parse("match", Picks, "low", MATCH_PROMPT, [{"type": "text", "text": text}])

    def suggest_week(self, context: dict, from_stock: bool = False) -> WeekSuggestions:
        return self._parse("week", WeekSuggestions, "medium", STOCK_WEEK_PROMPT if from_stock else WEEK_PROMPT,
                           [{"type": "text", "text": json.dumps(context, default=str)}])
