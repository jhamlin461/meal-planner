# Meal Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One web app (Android PWA + iPad web clip) for a two-person household that imports recipes, shows them as Cooking-for-Engineers tables, tracks pantry stock/expiry via barcode scans, plans the week, keeps an always-correct shopping list, and hands it to Walmart — with Grocy as the headless backend.

**Architecture:** FastAPI + Jinja2 server-rendered app talking to Grocy's REST API (Grocy is the single source of truth; extra data in Grocy userfields). Claude (Anthropic API) does recipe structuring, product mapping, and suggestions. Both containers run in one Proxmox LXC via Docker Compose, reached over Tailscale HTTPS. HA reads Grocy via its built-in integration and a small `/api/expiring` endpoint.

**Tech Stack:** Python ≥3.12, FastAPI, Jinja2, uvicorn, httpx (Grocy/OFF/USDA/UPCitemdb), `anthropic` 1.x SDK (model `claude-opus-5-5`), pydantic, pytest; vanilla JS with vendored `@zxing/browser@0.1.5`; Docker Compose; linuxserver/grocy.

**Spec:** `docs/superpowers/specs/2026-11-18-meal-planner-design.md` — read it before starting any task.

## Global Constraints

- Grocy is the only store for products, stock, recipes, meal plan, shopping lists. The only app-owned files: unreviewed imports in `DATA_DIR/pending/` and recipe source PDFs in `DATA_DIR/sources/`.
- No in-app login; the tailnet is the security boundary.
- Every Claude call: `client.beta.messages.parse(model="claude-opus-5-5", betas=["server-side-fallback-2026-07-01"], fallbacks="default", output_format=<pydantic model>, output_config={"effort": ...}, max_tokens=16000, ...)`; read `.parsed_output`; if `stop_reason == "refusal"` raise `AIError`. No `thinking` param (Opus 5.5 is always adaptive), no `temperature`, no forced `tool_choice`. Do not pass `httpx` objects to the anthropic client (SDK 1.x uses `httpx2`).
- Config only via env vars (see Task 1): `GROCY_URL`, `GROCY_API_KEY`, `ANTHROPIC_API_KEY`, `FDC_API_KEY` (default `DEMO_KEY`), `WALMART_STORE_ID` (optional), `SHOPPING_WEEKDAY` (0=Mon…6=Sun, default `6`), `DATA_DIR` (default `/data`), `EXPIRY_WARN_DAYS` (default `3`).
- Household week: Sun–Thu dinners default **4** servings (dinner + next-day lunch leftovers), Fri/Sat dinner **2**, Sat/Sun lunch **2**. Weekday lunches are leftovers only.
- Grocy shopping lists: **"Meal plan"** (app-owned, rebuilt from scratch) and **"Extras"** (never touched by the app).
- Walmart cart link format, exactly: `https://www.walmart.com/sc/cart/addToCart?items=<id>[_<qty>],...[&storeId=<id>]` (omit `_qty` when qty is 1).
- All pages usable at 360 px wide (Android) and in iPad landscape; recipe view uses Screen Wake Lock.
- Tests never hit the network: Grocy via `tests/fakes.py::FakeGrocy`, HTTP via `httpx.MockTransport`, Claude via `FakeAI`.

## Review Focus

1. **Shopping list rebuilt while someone is mid-shop** — items already ticked `done` in "Meal plan" must stay ticked after a rebuild if the product is still needed (Task 6 test `test_rebuild_preserves_done_flags`).
2. **Meal plan points at a deleted recipe** — rebuild and suggestions skip it instead of crashing (Task 6 test `test_rebuild_skips_missing_recipe`).
3. **"Cooked it" tapped twice** — second tap is a no-op, stock not deducted twice (Task 7 test `test_cook_twice_is_noop`).
4. **Imported ingredient whose unit can't convert to the product's stock unit** ("1 can" of tomatoes stocked in g) — save is blocked until the user supplies the conversion (Task 11 test `test_save_blocks_unconvertible_unit`).
5. **Open Food Facts product with no `quantity`/nutrition** — product still created (package 1 piece, macros flagged missing), never a crash (Task 9 test `test_resolve_off_without_quantity`).

---

## File Structure

```
pyproject.toml
app/
  config.py         Settings dataclass from env
  grocy.py          Grocy REST client (thin)
  setup_grocy.py    idempotent bootstrap of units/locations/lists/sections/userfields
  qty.py            parse/format/scale quantities, unit normalisation, conversion to stock unit
  cfe.py            CfE table model, validation, grid layout
  walmart.py        item-id parsing, cart URL, package rounding
  shopping.py       week needs, shortfall, shopping window, list rebuild
  mealplan.py       default servings, plan/unplan with leftovers, cook entry
  ai.py             Claude wrapper + pydantic result models
  barcode.py        OFF / UPCitemdb lookups, resolve-and-create flow
  nutrition.py      macros calc, USDA FDC lookup
  importer.py       JSON-LD extraction, drafts, pending store, matching, save
  suggest.py        weekly suggestions
  web.py            FastAPI app + all routes
  templates/        base, pantry, scan, recipes, recipe, import, review, plan, shopping, _cfe_table, _cfe_editor
  static/           style.css, app.js, manifest.webmanifest, icons, vendor/zxing-browser.min.js
scripts/make_icons.py
tests/              fakes.py + test_<module>.py per module, fixtures/
deploy/             Dockerfile, docker-compose.yml, README.md, ha/configuration.yaml, ha/automation.yaml
```

---

### Task 1: Project skeleton, config, Grocy client

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `app/__init__.py`, `app/config.py`, `app/grocy.py`, `tests/test_grocy.py`, `tests/fakes.py`

**Interfaces:**
- Produces:
  - `Settings` (frozen dataclass) with fields matching Global Constraints env vars; `load_settings() -> Settings`.
  - `class Grocy(base_url: str, api_key: str, transport: httpx.BaseTransport | None = None)` — `base_url` ends in `/api`; sends header `GROCY-API-KEY`. Methods:
    - `objects(entity: str, **where) -> list[dict]` → `GET /objects/{entity}` with `query[]=k=v` per kwarg
    - `get_object(entity, id) -> dict | None`
    - `create(entity: str, data: dict) -> int` → `POST /objects/{entity}`, returns `int(created_object_id)`
    - `update(entity, id, data) -> None` (PUT), `delete(entity, id) -> None`
    - `userfields(entity, id) -> dict`, `set_userfields(entity, id, data) -> None` (`/userfields/{entity}/{id}`)
    - `product_by_barcode(code: str) -> dict | None` (`GET /stock/products/by-barcode/{code}`; HTTP 400 → `None`)
    - `stock_amounts() -> dict[int, float]` — own amounts from `GET /stock`, then each child's amount added to its parent (`products.parent_product_id`), so parents carry rolled-up totals
    - `stock_entries() -> list[dict]` (`/objects/stock`)
    - `add_stock(product_id, amount, best_before: date | None, location_id: int | None) -> None` (`POST /stock/products/{id}/add`)
    - `consume(product_id, amount) -> None` (`POST /stock/products/{id}/consume`, body `{"amount", "spoiled": false, "allow_subproduct_substitution": true}`)
    - `set_amount(product_id, new_amount) -> None` (`POST /stock/products/{id}/inventory`)
    - `volatile(days: int) -> dict` (`GET /stock/volatile?due_soon_days=`)
  - `GrocyError(Exception)` raised on non-2xx (except the by-barcode 400 case).
  - `tests/fakes.py::FakeGrocy` — in-memory implementation of every method above (dict of entity → {id: row}; `stock` dict product_id → list of entries). Later tasks extend it.

- [ ] **Step 1:** `git init`; write `pyproject.toml` (deps: fastapi, uvicorn[standard], jinja2, python-multipart, httpx, anthropic>=1, pydantic>=2; dev: pytest), `.gitignore` (`.venv/`, `__pycache__/`, `data/`, `.env`). Run `uv sync`. Expected: lockfile created.
- [ ] **Step 2: Write failing tests** in `tests/test_grocy.py` using `httpx.MockTransport`:

```python
def test_create_returns_id_and_sends_key():
    seen = {}
    def handler(req):
        seen["key"] = req.headers["GROCY-API-KEY"]; seen["url"] = str(req.url)
        return httpx.Response(200, json={"created_object_id": "7"})
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.create("products", {"name": "Flour"}) == 7
    assert seen == {"key": "k", "url": "http://g/api/objects/products"}

def test_objects_builds_query():
    def handler(req):
        assert req.url.params.get_list("query[]") == ["name=Flour"]
        return httpx.Response(200, json=[{"id": 1}])
    assert Grocy("http://g/api", "k", transport=httpx.MockTransport(handler)).objects("products", name="Flour") == [{"id": 1}]

def test_product_by_barcode_unknown_is_none():
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error_message": "x"})))
    assert g.product_by_barcode("123") is None

def test_stock_amounts_roll_children_into_parent():
    def handler(req):
        if req.url.path.endswith("/stock"):
            return httpx.Response(200, json=[{"product_id": 2, "amount": "500"}, {"product_id": 1, "amount": "100"}])
        return httpx.Response(200, json=[{"id": 1, "parent_product_id": None}, {"id": 2, "parent_product_id": 1}])
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(handler))
    assert g.stock_amounts() == {1: 600.0, 2: 500.0}

def test_error_status_raises():
    g = Grocy("http://g/api", "k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(GrocyError):
        g.objects("products")
```

- [ ] **Step 3:** Run `uv run pytest tests/test_grocy.py -v` — Expected: FAIL (import error).
- [ ] **Step 4:** Implement `app/config.py` and `app/grocy.py` per Interfaces; write `FakeGrocy` with the same method names.
- [ ] **Step 5:** Run `uv run pytest -v` — Expected: PASS.
- [ ] **Step 6:** Commit `feat: project skeleton and Grocy client`.

---

### Task 2: Grocy bootstrap

**Files:**
- Create: `app/setup_grocy.py`, `tests/test_setup_grocy.py`

**Interfaces:**
- Consumes: `Grocy` / `FakeGrocy` (Task 1).
- Produces: `bootstrap(grocy) -> None` (idempotent), constants:
  - `UNITS = ["g", "kg", "ml", "l", "piece", "cup", "tbsp", "tsp", "oz", "lb", "fl oz", "can", "pinch"]`
  - `GLOBAL_CONVERSIONS = {("kg","g"): 1000, ("l","ml"): 1000, ("cup","ml"): 236.6, ("tbsp","ml"): 14.79, ("tsp","ml"): 4.93, ("fl oz","ml"): 29.57, ("oz","g"): 28.35, ("lb","g"): 453.6}`
  - `LOCATIONS = ["Fridge", "Freezer", "Pantry", "Spice rack", "Counter"]`
  - `SECTIONS = ["Lunch", "Dinner"]` (entity `meal_plan_sections`), `LISTS = ["Meal plan", "Extras"]` (entity `shopping_lists`)
  - Userfields (entity `userfields`, fields `entity, name, caption, type`): recipes → `cfe_table` (text-multi-line), `source_url` (text-single-line), `source_pdf` (text-single-line); products → `walmart_item_id` (text-single-line), `staple` (checkbox), `package_amount`, `g_per_unit`, `kcal_100`, `protein_100`, `carbs_100`, `fat_100` (number-decimal).
  - `ids(grocy) -> Ids` dataclass with `unit: dict[str,int]`, `location: dict[str,int]`, `section: dict[str,int]`, `list: dict[str,int]` — the lookup every later task uses for names → Grocy ids.

- [ ] **Step 1: Write failing tests:** `test_bootstrap_creates_everything` (after one run, FakeGrocy has 13 units, 8 global conversions with `product_id` None, 5 locations, 2 sections, 2 lists, 11 userfields) and `test_bootstrap_is_idempotent` (second run creates nothing new — counts unchanged).
- [ ] **Step 2:** Run — Expected: FAIL.
- [ ] **Step 3:** Implement: for each item, create only when no row with that name exists. Conversion rows: `quantity_unit_conversions` with `from_qu_id`, `to_qu_id`, `factor`, `product_id=None`. Also expose `python -m app.setup_grocy` that runs `bootstrap` with `load_settings()`.
- [ ] **Step 4:** Run — Expected: PASS.
- [ ] **Step 5:** Commit `feat: idempotent Grocy bootstrap`.

---

### Task 3: Quantities and units

**Files:**
- Create: `app/qty.py`, `tests/test_qty.py`

**Interfaces:**
- Produces:
  - `parse_qty(text: str) -> float | None` — handles `"2"`, `"1/2"`, `"1 1/2"`, `"1½"`, `"0.25"`, `""`→None.
  - `format_qty(x: float | None) -> str` — nearest of eighths/thirds within 0.02 → `"1 1/3"`, `"1/4"`, else 2 significant decimals stripped (`"2.5"`); None → `""`.
  - `normalize_unit(text: str) -> str` — maps aliases to `UNITS` names: `Tbs/Tbsp/tablespoon(s)/T`→`tbsp`, `tsp/teaspoon(s)/t`→`tsp`, `cups`→`cup`, `grams/gram`→`g`, `ounce(s)`→`oz`, `pound(s)/lbs`→`lb`, `mL/milliliter(s)`→`ml`, `""/each/large/medium/small/clove(s)/whole`→`piece`; unknown returned lower-cased.
  - `to_stock_amount(qty: float, unit: str, stock_unit: str, conversions: list[tuple[str, str, float]]) -> float | None` — conversions are `(from, to, factor)` already filtered to global + this product; tries identity, direct, reverse (1/factor), then one intermediate hop; None if no path.

- [ ] **Step 1: Write failing tests:**

```python
def test_parse_qty():
    assert parse_qty("1 1/2") == 1.5 and parse_qty("1½") == 1.5 and parse_qty("1/4") == 0.25 and parse_qty("") is None

def test_format_qty():
    assert format_qty(1.3333) == "1 1/3" and format_qty(0.25) == "1/4" and format_qty(2.5) == "2 1/2" and format_qty(113.4) == "113"

def test_normalize_unit():
    assert normalize_unit("Tbs") == "tbsp" and normalize_unit("large") == "piece" and normalize_unit("Cups") == "cup"

def test_to_stock_amount_two_hops():
    conv = [("cup", "ml", 236.6), ("ml", "g", 0.53)]   # product-specific ml→g for flour
    assert round(to_stock_amount(1, "cup", "g", conv), 1) == 125.4

def test_to_stock_amount_reverse_and_missing():
    assert to_stock_amount(500, "g", "kg", [("kg", "g", 1000)]) == 0.5
    assert to_stock_amount(1, "can", "g", [("cup", "ml", 236.6)]) is None
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: quantity parsing, formatting and unit conversion`.

---

### Task 4: Cooking-for-Engineers table

**Files:**
- Create: `app/cfe.py`, `app/templates/_cfe_table.html`, `tests/test_cfe.py`

**Interfaces:**
- Consumes: `format_qty` (Task 3).
- Produces (pydantic models, also used as Claude output in Task 8):
  - `Ingredient(qty: float | None, unit: str, name: str, note: str = "")`
  - `Op(id: str, label: str, inputs: list[str])` — input refs are `"i<index>"` (ingredient) or another op's `id`
  - `CfeTable(pre: list[str], ingredients: list[Ingredient], ops: list[Op])`
  - `Cell(text: str, rowspan: int, colspan: int, kind: Literal["ingredient","op","filler"])`
  - `validate(t: CfeTable) -> list[str]` — error strings; valid ⇔ empty
  - `layout(t: CfeTable, scale: float = 1.0) -> list[list[Cell]]` — one list per ingredient row containing only cells that start in that row; raises `ValueError` if `validate` fails
  - `ingredient_text(ing: Ingredient, scale: float) -> str` — `"<format_qty(qty*scale)> <unit> <name>"`, dropping empty parts, `unit` omitted when `piece`
- Template `_cfe_table.html` takes `pre`, `grid`, `ncols`; pre rows render as one `<td colspan=ncols>`; when layout raises, the recipe page shows ingredients list + numbered `steps` instead (Task 13).

Validation rules (all must hold): every op id unique; every input ref exists; each ingredient is an input of exactly one op; each op is an input of at most one op; exactly one root op; each op's covered rows (union of its inputs' rows) are contiguous.

Layout algorithm (not determined by the tests alone):

```
rows(ref): "iK" → {K}; op → union of rows(input)
col(ref):  "iK" → 0;   op → 1 + max(col(input))
ncols = 1 + col(root)
row K, col 0: Cell(ingredient_text, 1, 1, "ingredient")
for each op: Cell(label, rowspan=len(rows(op)), colspan=1, "op") at (min(rows(op)), col(op))
for each op, its inputs in row order: an input with col(input) < col(op)-1 needs a filler at
  rows(input), cols col(input)+1 .. col(op)-1. Consecutive *ingredient* inputs needing the
  same column range are merged into one filler (rowspan = their combined rows).
```

- [ ] **Step 1: Write the failing test** with the brownie example from the spec:

```python
BROWNIE = CfeTable(
    pre=["Butter and flour an 8x8-in pan", "Preheat oven to 350°F (170°C)"],
    ingredients=[Ingredient(qty=4, unit="oz", name="unsalted butter"), Ingredient(qty=1, unit="cup", name="sugar"),
                 Ingredient(qty=0.25, unit="tsp", name="vanilla extract"), Ingredient(qty=4, unit="tbsp", name="espresso"),
                 Ingredient(qty=2, unit="piece", name="large eggs"), Ingredient(qty=0.5, unit="cup", name="all-purpose flour"),
                 Ingredient(qty=1/3, unit="cup", name="cocoa powder"), Ingredient(qty=0.25, unit="tsp", name="baking soda"),
                 Ingredient(qty=0.25, unit="tsp", name="table salt")],
    ops=[Op(id="o1", label="melt", inputs=["i0"]), Op(id="o2", label="mix", inputs=["o1", "i1", "i2", "i3"]),
         Op(id="o3", label="mix", inputs=["o2", "i4"]), Op(id="o4", label="fold in", inputs=["o3", "i5", "i6", "i7", "i8"]),
         Op(id="o5", label="bake 350°F 30 to 40 min", inputs=["o4"])])

def shape(grid): return [[(c.text, c.rowspan, c.colspan) for c in row] for row in grid]

def test_brownie_layout():
    g = shape(layout(BROWNIE))
    assert g[0] == [("4 oz unsalted butter", 1, 1), ("melt", 1, 1), ("mix", 4, 1), ("mix", 5, 1), ("fold in", 9, 1), ("bake 350°F 30 to 40 min", 9, 1)]
    assert g[1] == [("1 cup sugar", 1, 1), ("", 3, 1)]
    assert g[4] == [("2 large eggs", 1, 1), ("", 1, 2)]
    assert g[5] == [("1/2 cup all-purpose flour", 1, 1), ("", 4, 3)]
    assert g[8] == [("1/4 tsp table salt", 1, 1)]

def test_layout_scales_quantities():
    assert layout(BROWNIE, scale=2)[0][0].text == "8 oz unsalted butter"

def test_validate_catches_unused_and_noncontiguous():
    bad = BROWNIE.model_copy(update={"ops": [Op(id="o1", label="mix", inputs=["i0", "i2"])]})
    errs = validate(bad)
    assert any("contiguous" in e for e in errs) and any("i1" in e for e in errs)

def test_repeated_ingredient_rows_are_valid():
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="cup", name="sugar"), Ingredient(qty=2, unit="piece", name="eggs"),
                                      Ingredient(qty=1, unit="tbsp", name="sugar")],
                 ops=[Op(id="a", label="whip", inputs=["i0", "i1"]), Op(id="b", label="sprinkle", inputs=["a", "i2"])])
    assert validate(t) == []
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement `cfe.py` and the partial template. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: Cooking for Engineers table model and layout`.

---

### Task 5: Walmart hand-off helpers

**Files:**
- Create: `app/walmart.py`, `tests/test_walmart.py`

**Interfaces:**
- Produces:
  - `parse_item_id(url_or_id: str) -> str | None` — accepts a bare numeric id or any `walmart.com/ip/<slug>/<id>` / `walmart.com/ip/<id>` URL (query string ignored).
  - `packages_needed(shortfall: float, package_amount: float | None) -> int` — `ceil(shortfall / package_amount)`, minimum 1; `package_amount` None/0 → 1.
  - `cart_url(items: list[tuple[str, int]], store_id: str | None) -> str`
  - `search_url(name: str) -> str` → `https://www.walmart.com/search?q=<urlencoded>`

- [ ] **Step 1: Write failing tests:**

```python
def test_parse_item_id():
    assert parse_item_id("https://www.walmart.com/ip/Great-Value-All-Purpose-Flour-5-lb/10535106?athbdg=L1600") == "10535106"
    assert parse_item_id("10535106") == "10535106" and parse_item_id("https://example.com/x") is None

def test_cart_url_matches_walmart_docs():
    assert cart_url([("945193065", 1), ("660768274", 2)], None) == "https://www.walmart.com/sc/cart/addToCart?items=945193065,660768274_2"
    assert cart_url([("938038697", 3)], "5435") == "https://www.walmart.com/sc/cart/addToCart?items=938038697_3&storeId=5435"

def test_packages_needed():
    assert packages_needed(300, 2268) == 1 and packages_needed(2500, 2268) == 2 and packages_needed(0.2, None) == 1
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: Walmart cart link helpers`.

---

### Task 6: Shopping list engine

**Files:**
- Create: `app/shopping.py`, `tests/test_shopping.py`; Modify: `tests/fakes.py` (seed helpers)

**Interfaces:**
- Consumes: `Grocy` (Task 1), `ids()` (Task 2), `packages_needed` (Task 5).
- Produces:
  - `shopping_window(today: date, shopping_weekday: int) -> tuple[date, date]` — start = today; next_shop = first date ≥ today with that weekday; end = next_shop + 7 days (inclusive).
  - `week_needs(entries: list[dict], positions: dict[int, list[dict]], base_servings: dict[int, float]) -> dict[int, float]` — entries are Grocy `meal_plan` rows; only `type == "recipe"` and `done` falsy count; need += `pos.amount * entry.recipe_servings / base_servings[recipe_id]`; entries whose recipe is missing from `positions` are skipped.
  - `shortfall(needs: dict[int, float], stock: dict[int, float], staples: set[int]) -> dict[int, float]` — `max(0, need - stock)`; staples count only when stock is 0.
  - `rebuild_meal_plan_list(grocy, today: date, shopping_weekday: int) -> dict[int, float]` — reads meal plan rows in window, recipes, `recipes_pos`, products (for `staple` userfield), `stock_amounts()`; deletes every row of the "Meal plan" list; writes one `shopping_list` row per shortfall product (`amount` in stock units). A product that had `done=1` before and is still short is re-written with `done=1`. Returns the shortfall.
  - `walmart_items(grocy, shortfall: dict[int, float]) -> tuple[list[tuple[str, int]], list[dict]]` — (cart pairs for products with `walmart_item_id`, remaining products without one).

- [ ] **Step 1: Write failing tests:**

```python
def test_shopping_window_wednesday_sunday_shop():
    assert shopping_window(date(2026, 11, 18), 6) == (date(2026, 11, 18), date(2026, 11, 29))
    assert shopping_window(date(2026, 11, 22), 6) == (date(2026, 11, 22), date(2026, 11, 29))

def test_two_recipes_sharing_flour_sum_before_subtracting():
    entries = [{"type": "recipe", "recipe_id": 1, "recipe_servings": 4, "done": 0},
               {"type": "recipe", "recipe_id": 2, "recipe_servings": 2, "done": 0}]
    positions = {1: [{"product_id": 10, "amount": 150}], 2: [{"product_id": 10, "amount": 300}]}
    needs = week_needs(entries, positions, {1: 2, 2: 2})
    assert needs == {10: 600}
    assert shortfall(needs, {10: 500}, set()) == {10: 100}

def test_staple_only_when_out():
    assert shortfall({5: 2}, {5: 1}, {5}) == {} and shortfall({5: 2}, {5: 0}, {5}) == {5: 2}

def test_rebuild_removes_unplanned_and_keeps_extras(fake_grocy_with_week): ...
    # plan recipe → rebuild → item present; delete plan entry → rebuild → "Meal plan" empty; "Extras" row untouched

def test_rebuild_preserves_done_flags(fake_grocy_with_week): ...
    # mark Meal plan row done=1 → rebuild → same product row still done=1

def test_rebuild_skips_missing_recipe(fake_grocy_with_week): ...
    # meal_plan row with recipe_id 999 → rebuild runs, returns shortfall for the valid recipes only
```

Build `fake_grocy_with_week` as a pytest fixture in `tests/fakes.py` (bootstrapped FakeGrocy + flour product id 10, recipe 1 with base_servings 2 needing 150 g flour, stock 100 g, a meal plan entry on 2026-11-19 Dinner servings 4, and one "Extras" row).

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: shopping list rebuilt from the whole week`.

---

### Task 7: Meal plan and cooking

**Files:**
- Create: `app/mealplan.py`, `tests/test_mealplan.py`

**Interfaces:**
- Consumes: `Grocy`, `ids()`, `rebuild_meal_plan_list` (Task 6), `week_needs`.
- Produces:
  - `default_servings(day: date, section: str) -> int` — 4 for Dinner on Sun–Thu, else 2.
  - `LEFTOVER_PREFIX = "Leftovers: "`
  - `plan_meal(grocy, day: date, section: str, recipe_id: int, servings: int | None, settings) -> int` — creates `meal_plan` row (`type="recipe"`, `section_id`, `recipe_servings = servings or default_servings`); if Dinner on Sun–Thu also creates a `type="note"` row on day+1 Lunch with note `LEFTOVER_PREFIX + recipe name`; then calls `rebuild_meal_plan_list(grocy, date.today(), settings.shopping_weekday)`; returns entry id.
  - `unplan_meal(grocy, entry_id: int, settings) -> None` — deletes the entry and, for a dinner, the matching leftover note on day+1 Lunch; rebuilds the list.
  - `cook(grocy, entry_id: int) -> list[str]` — no-op returning `[]` if entry `done`; else for each recipe position consumes `min(need, stock)` of the product, sets entry `done=1`, returns names of products that were short.
  - `week_view(grocy, monday: date) -> list[DayPlan]` where `DayPlan(day: date, lunch: list[dict], dinner: list[dict])`.

- [ ] **Step 1: Write failing tests:** `test_default_servings` (Sun 2026-11-22 Dinner → 4, Fri 2026-11-20 Dinner → 2, Sat Lunch → 2); `test_plan_dinner_adds_leftover_lunch_next_day` (plan on Wed 2026-11-18 → note "Leftovers: Brownies" on 2026-11-19 Lunch, recipe_servings 4); `test_friday_dinner_no_leftover`; `test_unplan_removes_leftover_and_list_items`; `test_cook_deducts_scaled_amounts_and_reports_short` (stock 100 g, need 300 g → consumes 100, returns `["Flour"]`, entry done); `test_cook_twice_is_noop` (second call returns `[]`, stock unchanged).
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: meal planning with leftovers and cooking`.

---

### Task 8: Claude wrapper

**Files:**
- Create: `app/ai.py`, `tests/test_ai.py`; Modify: `tests/fakes.py` (add `FakeAI`)

**Interfaces:**
- Consumes: `CfeTable`, `Ingredient`, `Op` (Task 4).
- Produces:
  - Models: `RecipeDraft(title: str, servings: int | None, steps: list[str], table: CfeTable)`; `ProductGuess(generic_name: str, is_new_generic: bool, stock_unit: Literal["g","ml","piece"], package_amount: float | None, location: str, best_before_days: int, g_per_unit: float)`; `ConversionGuess(factor: float, explanation: str)`; `Suggestion(day: date, section: Literal["Lunch","Dinner"], recipe_id: int, reason: str)`; `WeekSuggestions(items: list[Suggestion])`; `IngredientPick(index: int, product_id: int | None)`; `Picks(items: list[IngredientPick])`.
  - `class AI(client: anthropic.Anthropic)` with methods (each one `parse` call per Global Constraints):
    - `structure_recipe(text: str | None = None, pdf: bytes | None = None, image: bytes | None = None) -> RecipeDraft` — effort `"high"`; system prompt explains the CfE format (ingredients in the order they are combined, ops reference `i<index>`/op ids, one root op, oven preheat/pan prep go in `pre`, split ingredients become repeated rows with partial amounts); PDF sent as base64 `document` block, image as base64 `image` block, before the text block.
    - `guess_product(info: dict, generics: list[str], locations: list[str], image: bytes | None = None) -> ProductGuess` — effort `"low"`; with `image`, Claude reads the package photo (used when no barcode database knows the item).
    - `guess_conversion(product_name: str, from_unit: str, to_unit: str) -> ConversionGuess` — effort `"low"`.
    - `match_ingredients(names: list[str], products: list[dict]) -> Picks` — effort `"low"`; `product_id` must be one of the given ids or null.
    - `suggest_week(context: dict) -> WeekSuggestions` — effort `"medium"`.
  - `AIError(Exception)`.
  - `FakeAI` in `tests/fakes.py` returning canned objects set per test.

- [ ] **Step 1: Write failing test** with a stub client object whose `beta.messages.parse(**kw)` records `kw` and returns `SimpleNamespace(stop_reason="end_turn", parsed_output=<model>)`:
  - `test_structure_recipe_sends_pdf_and_uses_fallbacks` — asserts `kw["model"] == "claude-opus-5-5"`, `kw["fallbacks"] == "default"`, `"server-side-fallback-2026-07-01" in kw["betas"]`, `kw["output_format"] is RecipeDraft`, first content block type `"document"` with `media_type == "application/pdf"`, and `"thinking" not in kw`.
  - `test_refusal_raises` — stub returns `stop_reason="refusal"` → `AIError`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: Claude wrapper with structured outputs`.

---

### Task 9: Barcode resolution

**Files:**
- Create: `app/barcode.py`, `tests/test_barcode.py`, `tests/fixtures/off_flour.json`

**Interfaces:**
- Consumes: `Grocy`, `ids()`, `AI.guess_product`, `qty.parse_qty`.
- Produces:
  - `off_lookup(code: str, http: httpx.Client) -> dict | None` — `GET https://world.openfoodfacts.org/api/v2/product/{code}.json`; `status != 1` → None; returns `{"name", "brand", "categories", "quantity_text", "kcal_100", "protein_100", "carbs_100", "fat_100"}` from `product_name`, `brands`, `categories_tags`, `quantity`, `nutriments["energy-kcal_100g"|"proteins_100g"|"carbohydrates_100g"|"fat_100g"]` (missing → None). Send `User-Agent: MealPlanner/1.0` (generic, no personal data).
  - `upc_lookup(code, http) -> dict | None` — `GET https://api.upcitemdb.com/prod/trial/lookup?upc={code}`; first item's `title`, `brand`; nutrition None.
  - `Resolved(product_id: int, name: str, generic_name: str, created: bool)`
  - `resolve(grocy, ai, http, code: str) -> Resolved | None` — existing barcode → that product (`created=False`); else OFF → UPCitemdb → None (UI then asks for a photo). On a hit: `ai.guess_product(info, generics, LOCATIONS)`; find or create the generic parent (stock unit, location, `default_best_before_days`, userfields `package_amount`, `g_per_unit`, nutrition copied from OFF only if the parent has none); create the child product (`parent_product_id`, same units/location) with nutrition userfields; create `product_barcodes` row `{product_id, barcode}`.
  - `resolve_from_photo(grocy, ai, http, code: str, image: bytes) -> Resolved` — calls `ai.guess_product({"barcode": code}, generics, LOCATIONS, image=image)`, then the same create path (child named from the guess's `generic_name` + " (scanned)" when no product name is known).
  - When a guess has `package_amount` None, store `package_amount = 1` on the product.
  - `relink(grocy, product_id: int, generic_name: str) -> None` — the one-tap fix: re-parent the child to the named generic (create it if missing).

- [ ] **Step 1: Write failing tests:** `test_known_barcode_returns_existing`; `test_resolve_creates_parent_child_and_barcode` (OFF fixture for "Great Value All Purpose Flour 5 lb", FakeAI guess generic "All-purpose flour" g/2268/Pantry/365 → two products, child's `parent_product_id` = parent id, barcode row, parent `kcal_100 == 364`); `test_resolve_off_without_quantity` (OFF JSON missing `quantity` and `nutriments`, guess `package_amount=None` → products created, `package_amount` userfield 1, nutrition userfields empty, no exception); `test_unknown_everywhere_returns_none`; `test_relink_moves_child`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: automatic barcode resolution`.

---

### Task 10: Macros

**Files:**
- Create: `app/nutrition.py`, `tests/test_nutrition.py`

**Interfaces:**
- Consumes: `Grocy`.
- Produces:
  - `Macros(kcal: float, protein: float, carbs: float, fat: float, missing: list[str])`
  - `recipe_macros(positions: list[dict], product_info: dict[int, dict], base_servings: float) -> Macros` — per serving; for each position grams = `amount * g_per_unit` (stock unit g → g_per_unit 1; ml → treated as g); value = grams/100 × `*_100`; a product with no `kcal_100` goes into `missing` by name and contributes 0.
  - `fdc_lookup(name: str, api_key: str, http) -> dict | None` — `GET https://api.nal.usda.gov/fdc/v1/foods/search?query=<name>&dataType=Foundation,SR%20Legacy&pageSize=1&api_key=<key>`; nutrient numbers `1008` kcal, `1003` protein, `1005` carbs, `1004` fat → `{"kcal_100", ...}`.
  - `fill_missing_nutrition(grocy, product_id, api_key, http) -> bool` — used when a generic is created without a barcode (import flow).

- [ ] **Step 1: Write failing tests:** `test_recipe_macros_per_serving` (100 g flour at 364 kcal/100 g + 2 eggs piece g_per_unit 50 at 143 kcal/100 g, base servings 2 → kcal 253.5) and `test_missing_nutrition_listed`; `test_fdc_lookup_parses_nutrients` with MockTransport.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: macros from product nutrition`.

---

### Task 11: Recipe import (URL, PDF, bulk queue, review, save)

**Files:**
- Create: `app/importer.py`, `tests/test_importer.py`, `tests/fixtures/recipe_jsonld.html`, `tests/fixtures/recipe_graph.html`

**Interfaces:**
- Consumes: `AI.structure_recipe`, `AI.match_ingredients`, `AI.guess_conversion`, `qty.normalize_unit`, `qty.to_stock_amount`, `cfe.validate`, `fill_missing_nutrition`, `Grocy`, `ids()`.
- Produces:
  - `extract_jsonld_recipe(html: str) -> str | None` — finds `<script type="application/ld+json">`, handles a top-level list and `@graph`, `@type` either `"Recipe"` or a list containing it; returns plain text: name, `recipeYield`, `recipeIngredient` lines, `recipeInstructions` flattened (strings, `HowToStep.text`, `HowToSection.itemListElement`).
  - `page_text(html: str) -> str` — stdlib `html.parser` text extraction, scripts/styles removed (fallback input).
  - `Pending(id: str, status: Literal["processing","ready","failed"], source: str, draft: RecipeDraft | None, error: str | None)`; `PendingStore(dir: Path)` with `new(source) -> Pending`, `save(p)`, `get(id)`, `all() -> list[Pending]`, `delete(id)`; one JSON file per id plus optional `<id>.pdf`.
  - `process(store, ai, http, pending_id: str, url: str | None, pdf: bytes | None) -> None` — fetch URL → JSON-LD text or page text → `structure_recipe`; or PDF → `structure_recipe(pdf=...)`; servings None → 2; status `ready`, or `failed` with the error message (never raises).
  - `IngredientMatch(index: int, product_id: int | None, stock_amount: float | None)`
  - `propose_matches(grocy, ai, draft) -> list[IngredientMatch]` — difflib `get_close_matches` (cutoff 0.85) against generic product names first, Claude for the rest; `stock_amount` via `to_stock_amount` with global + product conversions, None when no path.
  - `add_conversion(grocy, product_id, from_unit, factor) -> None` — creates product-specific `quantity_unit_conversions` row to the product's stock unit.
  - `save_recipe(grocy, draft, matches, source_url: str | None, pdf_path: Path | None) -> int` — raises `ValueError` listing ingredient indexes when any match has `product_id` or `stock_amount` None, or `validate(draft.table)` is non-empty; else creates `recipes` (`name`, `base_servings`, `description` = numbered steps HTML), one `recipes_pos` per ingredient (`product_id`, `amount` = stock amount, `qu_id` = product stock unit, `note` = original ingredient text), userfields `cfe_table` (`draft.table.model_dump_json()`), `source_url`, `source_pdf`.

- [ ] **Step 1: Write failing tests:** `test_extract_jsonld_simple`, `test_extract_jsonld_graph_and_howtosection`, `test_extract_jsonld_absent_returns_none`; `test_process_url_marks_ready` and `test_process_failure_marks_failed` (FakeAI raises) and `test_missing_servings_defaults_to_2`; `test_propose_matches_exact_then_ai`; `test_save_blocks_unconvertible_unit` (ingredient "1 can tomatoes", product stocked in g, no conversion → `ValueError` mentioning index; after `add_conversion(..., "can", 411)` re-propose gives 411 and save succeeds); `test_save_writes_positions_and_cfe_userfield`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: recipe import with review queue`.

---

### Task 12: Weekly suggestions

**Files:**
- Create: `app/suggest.py`, `tests/test_suggest.py`

**Interfaces:**
- Consumes: `AI.suggest_week`, `Grocy`, `week_needs`/`shortfall` (Task 6), `default_servings`.
- Produces:
  - `week_slots(monday: date) -> list[tuple[date, str]]` — 7 dinners + Sat/Sun lunch = 9 slots, minus slots already planned.
  - `build_context(grocy, monday) -> dict` — recipes (id, name, ingredient names, `fulfillment` = share of positions fully in stock), stock list with `best_before_date` for items due within 7 days, recipe names planned in the previous 3 weeks, open slots.
  - `suggest(grocy, ai, monday: date) -> list[Suggestion]` — drops suggestions whose `recipe_id` isn't a real recipe or whose slot isn't open.

- [ ] **Step 1: Write failing tests:** `test_week_slots_count_and_skips_planned` (empty week → 9; with Mon dinner planned → 8); `test_suggest_drops_invalid` (FakeAI returns one valid, one unknown recipe id, one already-planned slot → only the valid one).
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: weekly meal suggestions`.

---

### Task 13: Web app — layout, pantry, scanning, recipes

**Files:**
- Create: `app/web.py`, `app/templates/{base,pantry,scan,recipes,recipe,import,review}.html`, `app/static/{style.css,app.js,manifest.webmanifest}`, `app/static/vendor/zxing-browser.min.js`, `scripts/make_icons.py`, `app/static/icons/{icon-192.png,icon-512.png,apple-touch-icon.png}`, `tests/test_web.py`

**Interfaces:**
- Consumes: everything above. `create_app(grocy, ai, http, settings, store) -> FastAPI` (dependencies injected so tests pass `FakeGrocy`/`FakeAI`); module-level `app = create_app(...)` built from `load_settings()` only when run by uvicorn (`app.web:main_app` factory with `--factory`).
- Routes (all HTML unless noted):
  - `GET /` → redirect `/plan`
  - `GET /pantry` — "Use soon" (from `volatile(EXPIRY_WARN_DAYS)`) then stock grouped by location, sorted by best-before; each row has Use / Edit amount / Move forms
  - `POST /pantry/{product_id}/consume|amount|move` → redirect back
  - `GET /scan` — camera view; `POST /api/scan` (JSON `{code, action: "add"|"use"}`) → JSON `{status: "ok", product, generic, created}` or `{status: "unknown"}`; `POST /api/scan/photo` (multipart `code`, `image`); `POST /api/relink` (`{product_id, generic_name}`)
  - `GET /recipes` (list + import entry), `GET /recipes/{id}?servings=N` — CfE table (fallback list+steps), servings stepper, macros per serving with "macros incomplete: …" note, "Add to plan" form
  - `GET /import`, `POST /import` (multipart: many `files` and/or newline-separated `urls`; each becomes a `Pending` processed via `BackgroundTasks`), `GET /import/{id}` review screen (ingredient → product select, amount in stock units, conversion prompt when None, CfE preview + table editor), `POST /import/{id}` save (re-renders with errors on `ValueError`), `POST /import/{id}/discard`
  - CfE table editor (spec: "operation label + which rows it covers"), shared partial `_cfe_editor.html` used on the review screen and at `GET/POST /recipes/{id}/table`: one line per op with a label input and an inputs field (comma-separated refs like `o1, i1, i2`), plus "add op"; on submit the table is rebuilt, `validate` errors are shown inline, and only a valid table is saved to the `cfe_table` userfield
  - Error handler: `GrocyError` or `httpx.ConnectError` → 503 page "Can't reach Grocy — nothing was saved. Try again in a minute."
- `app.js`: scanner using `ZXingBrowser.BrowserMultiFormatReader().decodeFromVideoDevice(undefined, videoEl, cb)` with rear camera; on decode posts to `/api/scan`, shows a toast "Added <name> → <generic> · Fix" for 8 s (Fix opens a generic-name picker → `/api/relink`); `{status:"unknown"}` opens the photo capture (`<input type=file accept=image/* capture=environment>`). Wake lock: on pages with `data-wakelock`, `navigator.wakeLock?.request("screen")`, re-requested on `visibilitychange`.
- `base.html`: viewport meta, manifest link, `apple-touch-icon`, `apple-mobile-web-app-capable=yes`, bottom nav (Plan · Pantry · Scan · Recipes · Shopping); CSS: single column ≤ 700 px, CfE table horizontally scrollable on phones, large type (≥ 20 px table text) in landscape ≥ 1000 px.

- [ ] **Step 1:** Download `https://cdn.jsdelivr.net/npm/@zxing/browser@0.1.5/umd/zxing-browser.min.js` into `app/static/vendor/` (own directory, not executed at build). Expected: file > 100 KB. Write `scripts/make_icons.py` (stdlib `zlib`+`struct` solid-colour PNGs with a white "M") and run it.
- [ ] **Step 2: Write failing tests** with FastAPI `TestClient` and fakes: `test_pages_render` (each GET above returns 200), `test_recipe_page_has_cfe_table_and_wakelock` (brownie recipe saved via `save_recipe` → HTML contains `rowspan="9"` and `data-wakelock`), `test_recipe_servings_scales` (`?servings=4` on base 2 → "8 oz unsalted butter"), `test_scan_known_add_increments_stock`, `test_scan_unknown_returns_unknown`, `test_import_creates_pending_and_review_save` (FakeAI draft → POST /import → GET review → POST save → recipe exists), `test_table_edit_rejects_invalid_and_saves_valid` (POST an op list leaving `i1` unused → 200 with the validation error, userfield unchanged; valid list → userfield updated), `test_grocy_down_shows_503`.
- [ ] **Step 3:** Run — FAIL. **Step 4:** Implement. **Step 5:** Run — PASS.
- [ ] **Step 6:** Commit `feat: web UI for pantry, scanning, recipes and import`.

---

### Task 14: Web app — plan and shopping

**Files:**
- Create: `app/templates/{plan,shopping}.html`; Modify: `app/web.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `week_view`, `plan_meal`, `unplan_meal`, `cook`, `suggest`, `rebuild_meal_plan_list`, `walmart_items`, `cart_url`, `search_url`, `parse_item_id`.
- Routes:
  - `GET /plan?week=YYYY-MM-DD` (Monday) — 7 day cards; dinner slot + Sat/Sun lunch slot; weekday lunches show the leftover note; each recipe entry has servings, "Cooked it", remove; "Suggest week" button
  - `POST /plan` (`day, section, recipe_id, servings?`), `POST /plan/{entry_id}/remove`, `POST /plan/{entry_id}/cook` (flash "You were short on: …" when the list is non-empty, with a link to Pantry to correct), `POST /plan/suggest` → page listing suggestions with reasons and per-item Accept (→ `POST /plan`)
  - `GET /shopping` — "Meal plan" items (name, amount + unit, packages) and "Extras" items, tick boxes (`POST /shopping/{row_id}/toggle`), add-extra form, "Bought" (`POST /shopping/bought`: every ticked row → `add_stock` of the full package amount(s) with default best-before, row deleted, then rebuild), "Open Walmart cart" link (`cart_url` with `WALMART_STORE_ID`) plus "Still to add on Walmart" list with `search_url` links and a paste-link form per product (`POST /products/{id}/walmart` → `parse_item_id` → userfield)
  - `GET /api/expiring` (JSON) → `{"count": n, "message": "Use soon: milk (Fri), spinach (Sat)"}` from `volatile(EXPIRY_WARN_DAYS)`

- [ ] **Step 1: Write failing tests:** `test_plan_post_adds_leftover_and_list_item`, `test_cook_route_flashes_shortage`, `test_shopping_shows_walmart_link_with_store` (product with `walmart_item_id` 10535106, shortfall 300 g, package 2268, store 5435 → page contains `addToCart?items=10535106&amp;storeId=5435`), `test_bought_moves_to_stock_and_rebuilds`, `test_walmart_paste_sets_userfield`, `test_api_expiring_message`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `feat: week planning and shopping UI with Walmart hand-off`.

---

### Task 15: Deployment (Proxmox LXC, Tailscale, Home Assistant)

**Files:**
- Create: `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/README.md`, `deploy/ha/configuration.yaml`, `deploy/ha/automation.yaml`, `tests/integration/test_grocy_live.py`

**Interfaces:**
- Consumes: the whole app; `python -m app.setup_grocy`.
- Produces:
  - Dockerfile: `python:3.12-slim`, install with uv from the lockfile, `CMD uvicorn app.web:main_app --factory --host 0.0.0.0 --port 8000`.
  - Compose: `grocy` (`lscr.io/linuxserver/grocy:latest`, volume `./grocy:/config`, port `9283:80` — LAN only, for HA), `mealplanner` (build `..`, env from `.env`, volume `./data:/data`, port `8000:8000`, `depends_on: grocy`, `GROCY_URL=http://grocy/api`), both `restart: unless-stopped`.
  - README, in order: create Debian 12 LXC (2 vCPU, 2 GB RAM, 16 GB disk, `features: nesting=1,keyctl=1`; add `lxc.cgroup2.devices.allow: c 10:200 rwm` and `lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file` to `/etc/pve/lxc/<id>.conf` for Tailscale); install Docker + Tailscale; `tailscale up`; enable MagicDNS + HTTPS in the tailnet admin; `tailscale serve --bg 8000`; `docker compose up -d`; open Grocy once at `http://<lxc-ip>:9283` (admin/admin → change password → create API key) — the only time Grocy's UI is used; put keys in `.env`; `docker compose run --rm mealplanner python -m app.setup_grocy`; Proxmox scheduled backup of the LXC; phone install (Chrome → Install app); iPad (Tailscale app, Safari → Share → Add to Home Screen).
  - HA: add the Grocy integration (UI: URL `http://<lxc-ip>`, port 9283, API key); `configuration.yaml` REST sensor `sensor.meal_planner_expiring` polling `http://<lxc-ip>:8000/api/expiring` every 30 min (`value_template: {{ value_json.count }}`, `json_attributes: [message]`); `automation.yaml` daily 08:00, when sensor > 0, `notify.mobile_app_<phone>` for both phones with the `message` attribute (device names filled in by the owner — the README says where to find them).
  - `tests/integration/test_grocy_live.py` — skipped unless `GROCY_URL` is set; runs `bootstrap`, creates a product, adds stock, consumes, checks `stock_amounts`, creates/deletes a meal plan row, writes and reads a userfield, then deletes what it created. This validates every Grocy endpoint assumption against the real server.

- [ ] **Step 1:** Write the files above.
- [ ] **Step 2:** `docker compose -f deploy/docker-compose.yml config` — Expected: valid, no errors.
- [ ] **Step 3 (on the LXC, with the owner):** follow README through `setup_grocy`; run `GROCY_URL=... GROCY_API_KEY=... uv run pytest tests/integration -v` — Expected: PASS. Fix any endpoint mismatches in `app/grocy.py` (and FakeGrocy) and re-run the full suite.
- [ ] **Step 4:** Commit `feat: Proxmox/Tailscale deployment and HA config`.

---

### Task 16: On-device acceptance

**Files:** Modify: `deploy/README.md` (record findings)

- [ ] **Step 1 (Android, Chrome):** install app; scan a real grocery barcode → product auto-created with correct generic; tap Fix once; scan an unknown barcode → photo path works.
- [ ] **Step 2 (Walmart):** with 2 products holding Walmart ids, tap "Open Walmart cart" on Android → record whether it opens the Walmart app or Chrome and whether both items and quantities land in the cart for the configured pickup store. If the cart is lost, change the link to open in Chrome (`target="_blank"` + note in README) and re-test.
- [ ] **Step 3 (iPad):** web clip opens full-screen; brownie-style recipe readable in landscape; screen stays awake for 5 min on a recipe.
- [ ] **Step 4:** import one EveryPlate PDF and one recipe URL end-to-end; plan a week; confirm the shopping list matches a hand count.
- [ ] **Step 5:** HA notification arrives on both phones (temporarily set the automation trigger a few minutes ahead).
- [ ] **Step 6:** Commit `docs: on-device acceptance results`.
