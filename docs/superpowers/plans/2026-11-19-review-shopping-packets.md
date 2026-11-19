# Hidden weights, packets, checked tables, Shopping tab and cooking from stock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The import review asks only for products (no weights), tables carry EveryPlate packets and pass a Claude
accuracy check, every amount a person sees is in a household measure they can change, Shopping is its own tab with
food Keep at least, a recipe's ingredients can be added to inventory, and the week can be suggested from stock.

**Architecture:** Product-level "household measure" data (measure word, factor to the stock unit, and where each value
came from) lives in Grocy product userfields behind a small `app/measure.py`; every page that shows or reads an amount
converts through it. Import gains a second Claude call (`AI.check_table`) whose corrections and doubts are stored on
the pending import. `cfe.Ingredient` gains `packets`. Review, recipe, inventory, scan, shopping and plan pages are
updated to match.

**Tech Stack:** FastAPI, Jinja2, vanilla JS, Grocy REST, Anthropic SDK (`beta.messages.parse` as today), pytest with
FakeGrocy/FakeAI.

**Spec:** `docs/superpowers/specs/2026-11-19-review-shopping-packets-design.md` (its "Calls made" section is binding).

## Global Constraints

- Load `frontend-design:frontend-design` before any UI work; follow the spec's Style section. Keep the
  Midnight/Black/Light tokens (`--on-action` for text on action fills; Black outlines surfaces).
- No new runtime dependencies. No inline styles (tests check). Targets ≥ 48 px; sentence case; no middle dots or arrows
  in UI text; no overlap or sideways scroll from 320 px.
- No gram/ml value is shown or typed anywhere a person cooks, shops or keeps stock (spec goals 2 and 11); stock units
  stay internal.
- Every Claude call goes through `app/ai.py`'s `AI` (metered, budget modes, `AIPaused` in free mode); a new call type
  gets an `ECONOMY` entry.
- Grocy field names must be Grocy's real ones; userfields are created in `app/setup_grocy.py`'s userfield list.
- Existing tests stay green except where a task changes a behaviour on purpose; update them, never drop coverage.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A review where every product is picked and nothing else typed must save (no hidden required number), in full,
   economy and free mode.
2. The check pass returning a table with a different number or order of rows/steps: rejected as a correction, kept
   as a doubt, never misaligning product picks.
3. Changing an item's measure, then Use/Set total/Keep at least: the stock written in the stock unit must equal what
   the person typed in their measure.
4. Shopping for a product on both the meal plan and below Keep at least: listed once, enough for both.
5. Packets at a non-base servings (e.g. 3 servings of a 2|4 card): "1 1/2 packets", and add-to-inventory books the
   matching measured amount.

---

### Task 1: Review shows only products; Review link; Create all missing

**Files:** `app/web.py` (review routes, recipes route), `app/importer.py` (Pending, estimates), `app/templates/review.html`,
`app/templates/recipes.html`, `app/templates/import.html` (if needed), `app/static/style.css`, tests
(`tests/test_web.py` review tests, new tests).

**Interfaces — produces:**
- `Pending.estimates: dict[str, float] = {}` — key `"{product_id}:{unit}"`, value = stock units per 1 `unit` (Claude's
  conversion guess, cached so a reload costs nothing).
- `importer.estimate_amount(grocy, ai, pending, product_id, qty, unit) -> float | None`: known conversion first
  (`stock_amount_for`), else the cached/new Claude estimate (`ai.guess_conversion`), `None` in free mode or on AIError.
  On Save, a used estimate is stored with `add_conversion` (its value source recorded as "estimate" once Task 4 exists;
  until then just stored).
- Review form: per row only the product select (+ Create / free-mode manual fields as today); amounts never rendered or
  posted. Packaging rows: amount from the card's measured qty (Task 2 adds packets) — no "enter the real amount" field.
- `POST /import/{id}` action `create_all` creates a product for every row without one (same path as `create:<i>`), in
  row order, and re-renders; per-row errors show on their rows.
- Recipes "Review N waiting" → `GET /import/next` → 303 to the oldest ready import's review, or `/import` when none.
  After Save or Discard of a review, redirect to `/import/next` semantics: the next ready import, else `/recipes`.
- Free mode: a row whose amount can't be worked out saves with no stock amount (see spec "Left to the builder"; pick the
  storage, e.g. the recipe position with amount 0 and a `needs_estimate` marker the importer fills later; cooking must
  not take it out of stock).

- [ ] Tests first (record RED): `test_review_shows_no_amounts_or_factors` (no "Uses", no `amount_`/`factor_` inputs, no
  "1 cup ="); `test_review_saves_with_only_products_picked` (full mode: Claude estimate used, conversion stored;
  cached on reload — FakeAI conversion called once); `test_review_free_mode_saves_without_amount`;
  `test_create_all_missing`; `test_review_waiting_link_opens_first_review`; `test_save_and_discard_go_to_next_waiting`.
- [ ] Implement; update existing review tests that posted amounts/factors. Full suite green.
- [ ] Commit: `feat: the import review asks only for products; Review opens the waiting recipe`.

### Task 2: Packets and recipetables-style wording

**Files:** `app/cfe.py` (Ingredient, ingredient_text, layout cells), `app/ai.py` (CFE_PROMPT), `app/templates/_cfe_table.html`,
`app/templates/recipe.html` (no Steps section when a table exists), `app/templates/_cfe_editor.html` (packets field per
ingredient row in the table editor), `app/static/style.css`, tests (`tests/test_cfe.py`, `tests/test_ai.py`,
`tests/test_web.py`).

**Interfaces — produces:**
- `cfe.Ingredient.packets: float | None = None` (packets for the recipe's base servings).
- `cfe.packet_text(n: float) -> str`: 1 → "1 packet", 2 → "2 packets", 0.5 → "half a packet", 1.5 → "1 1/2 packets"
  (fractions via `format_qty`).
- `ingredient_text(ing, scale)` unchanged in shape; the table/review/confirm templates render the packet pill from
  `ing.packets * scale` separately (cell data carries `packets`).
- CFE_PROMPT additions (wording rules from the spec goal 8, and): read meal-kit "what we send" columns; set `packets`
  for the column matching the recipe's servings; keep the measured amount (from the card, else estimated with "est."
  in the note); household staples get no packets.
- Recipe page: the plain-text Steps disclosure shows only when there is no valid table.

- [ ] Tests first: `test_packet_text`; `test_ingredient_packets_round_trip_and_scale` (2 servings card, packets 1,
  shown at 3 servings as "1 1/2 packets"); `test_table_cell_shows_packet_pill`; `test_prompt_mentions_packets_and_purpose_notes`;
  `test_recipe_page_hides_steps_when_table_exists`; `test_table_editor_keeps_packets`.
- [ ] Implement (frontend-design: amount bold tabular figures, muted purpose note, packet as a small outlined pill).
  Full suite green; check the recipe page at 390/1440 in Midnight and Black (demo server; give the demo brownie table a
  packet).
- [ ] Commit: `feat: tables show EveryPlate packets beside the measured amount`.

### Task 3: The accuracy check pass

**Files:** `app/ai.py` (new call), `app/importer.py` (process/_collect/make_table call it), `app/importer.py` Pending,
`app/web.py` (review render), `app/templates/review.html`, `app/templates/_cfe_table.html`, `app/static/style.css`,
`tests/fakes.py` (FakeAI), tests.

**Interfaces — produces:**
- `ai.TableCheck(BaseModel)`: `table: CfeTable`, `doubts: list[Doubt]`; `Doubt`: `ref: str` (`"i<index>"` or an op id),
  `reason: str`.
- `AI.check_table(table: CfeTable, servings: int | None, text=None, pdf=None, image=None) -> TableCheck`: call type
  `"check"` (full: same model/effort as `"recipe"`; `ECONOMY["check"]` = economy recipe settings). Prompt: compare
  every amount, unit, packet count, temperature, time and each step's inputs against the source; return the corrected
  table with the same rows and steps in the same order, and a doubt for anything uncertain, missing or extra.
- `importer.apply_check(table, check) -> tuple[CfeTable, list[Doubt]]`: accept the corrected table only if it has the
  same ingredient count and the same op ids in the same order and still validates; otherwise keep the original and add
  a doubt "The check suggested changes that didn't fit; compare with the card." Drop doubts with unknown refs.
- `Pending.doubts: list[dict]` (ref, reason). Process, batch collection and Make the table run the check (an AIError or
  AIPaused during the check keeps the unchecked table and adds no doubts).
- Review: doubtful cells get class `doubt` and a numbered marker; a numbered list of reasons under the table
  (`role="note"`). A doubt clears when that row/step is edited in the review form (compare submitted vs stored) and all
  clear on Save.

- [ ] Tests first: `test_check_corrects_table`; `test_check_rejects_reordered_table_as_doubt` (Review Focus 2);
  `test_check_failure_keeps_table`; `test_review_marks_doubts_and_lists_reasons`; `test_editing_a_row_clears_its_doubt`;
  `test_batch_and_make_table_run_check`; economy settings for `check`.
- [ ] Implement (frontend-design: dashed due-soon outline + numbered marker). Full suite green; check the review page
  with two doubts at 390/1440.
- [ ] Commit: `feat: a second Claude pass checks every table against the card`.

### Task 4: Household measures everywhere amounts show

**Files:** create `app/measure.py`; modify `app/setup_grocy.py` (userfields), `app/ai.py` (measure pick in
`ProductGuess` and a small `guess_measure` call), `app/barcode.py`, `app/importer.py` (`create_generic`), `app/inventory.py`
(rows carry display amounts), `app/web.py` (inventory forms, scan lookup/confirm, Keep at least), `app/templates/inventory.html`,
`app/templates/scan.html` + `app/static/app.js` (left line, amount field), tests (new `tests/test_measure.py`, inventory
and scan tests).

**Interfaces — produces:**
- Userfields on products: `measure` (text, e.g. "cups", "rolls"), `measure_factor` (stock units per 1 measure),
  `measure_source` ("estimate" | "link" | "owner"), `package_source` (same values; for `package_amount`).
- `measure.KITCHEN = {"cup": ("vol", 236.6), "tbsp": ("vol", 14.79), "tsp": ("vol", 4.93), "fl oz": ("vol", 29.57),
  "oz": ("wt", 28.35), "lb": ("wt", 453.6)}` (ml / g per unit).
- `measure.Measure(word: str, factor: float)` and `measure.of(grocy, ai, product) -> Measure`: the product's stored
  measure; else, when Claude is available, `ai.guess_measure(name, stock_unit)` once, stored with source "estimate";
  else the stock unit itself (factor 1).
- `measure.show(amount_stock, m) -> str` ("2 1/2 cups", "3 rolls", singular "1 roll"/"1 cup", via `format_qty`) and
  `measure.to_stock(amount, m) -> float`.
- `measure.choices(product, m) -> list[str]`: KITCHEN words plus the count word if the product is counted in pieces or
  has one; `measure.change(grocy, product, word)`: new factor via fixed kitchen math within vol/wt, else via the
  product's grams-per-unit / existing conversion; source "owner".
- Inventory: amounts, Use, Set total, and the supplies' existing Keep at least shown/typed in the measure (food Keep at
  least arrives in Task 6 and uses these helpers); More gains "Measure in [▾]". Scan lookup returns `measure` and `left` text; confirm's `amount` is in the measure.
- New products (scan, manual, review create, create all) get their measure at creation (`ProductGuess` gains
  `measure`, `measure_factor`).

- [ ] Tests first: `test_kitchen_math` (2 tbsp = 1/8 cup; 16 oz = 1 lb); `test_show_rounds_to_kitchen_fractions`;
  `test_measure_estimated_once_and_cached`; `test_change_measure_owner_wins`; `test_inventory_use_in_cups_writes_grams`
  (Review Focus 3); `test_keep_at_least_in_rolls`; `test_no_grams_on_inventory_or_scan` (asserts no " g left"/" ml left"
  for products with a measure); `test_free_mode_shows_stock_unit_until_measure`.
- [ ] Implement (frontend-design for the More "Measure in" control). Full suite green; check Inventory and the scan sheet
  at 390 in Black.
- [ ] Commit: `feat: every amount shows in the item's household measure, and it can be changed`.

### Task 5: Walmart links refine package size and measures

**Files:** `app/web.py` (`POST /products/{pid}/walmart`), `app/walmart.py`, `app/ai.py` (new `read_walmart` call),
`app/measure.py`, tests.

**Interfaces — produces:**
- `walmart.slug_text(link: str) -> str`: the product words from a walmart.com item URL slug ("Great-Value-Long-Grain-
  White-Rice-32-oz" → "Great Value Long Grain White Rice 32 oz"); empty for a bare item number.
- `walmart.page_title(http, item_id) -> str | None`: one plain GET of the item page (short timeout, the app's httpx
  client, UA as elsewhere); the `<title>` text or None on any failure/bot wall.
- `AI.read_walmart(name: str, stock_unit: str, text: str) -> WalmartRead` (`package_amount` in stock units | None,
  `grams_per_piece` | None, `measure_factor` | None — only when obvious).
- On linking: values fill blanks or replace sources "estimate"; never overwrite "owner"/"scan" values (spec call 2);
  set sources to "link". Free mode: link saved, no read. Failures are silent (link still saved).

- [ ] Tests first: `test_slug_text`; `test_link_refines_estimated_package_and_measure`; `test_link_never_overwrites_owner_values`;
  `test_link_saved_when_read_fails`.
- [ ] Implement; full suite green. Commit: `feat: linking a Walmart item corrects package size and measure`.

### Task 6: Shopping tab, food Keep at least, one line per product

**Files:** `app/templates/base.html` (tabs), `app/web.py` (sections), `app/shopping.py`, `app/templates/shopping.html`,
`app/templates/inventory.html` (food Keep at least in More), `app/inventory.py`, tests.

**Interfaces — produces:**
- Tabs: Today `/`, Meals `/plan`, Inventory `/inventory`, Shopping `/shopping`, Chores `/home`. Meals pages: Plan,
  Recipes. `/shopping` is its own section.
- Food rows' More: "Keep at least [n] {measure}" (sets the generic's min stock, cumulated across brands, as supplies).
- `shopping.combined_need(meal_plan: dict[int, float], running_low: dict[int, float], stock, mins) -> dict[int, float]`:
  per product, enough to cook the plan and still keep the minimum (`max(0, need + min - stock)`); the Shopping page
  shows each product once with that amount (in its measure, Task 4) and builds the Walmart cart from it.

- [ ] Tests first: `test_tabs_have_shopping`; `test_food_keep_at_least_puts_it_on_running_low`;
  `test_product_on_both_lists_shown_once_with_combined_amount` (Review Focus 4); existing Shopping tests updated.
- [ ] Implement (frontend-design for 5 tabs at 320 px). Full suite green. Commit: `feat: Shopping gets its own tab; food can keep a minimum`.

### Task 7: Add a recipe's ingredients to inventory

**Files:** `app/web.py`, `app/templates/recipe.html`, new `app/templates/recipe_stock.html`, `app/inventory.py` (or a small
helper), tests.

**Interfaces — produces:**
- Recipe page secondary action "Add ingredients to inventory" (uses the page's servings) → `GET /recipes/{rid}/stock?servings=`
  confirm page listing each ingredient as the table shows it ("2 tbsp umami ginger sauce (1 packet)") with its place,
  and "Skipped" lines for ingredients without a product or stock amount; button "Add to inventory"; Cancel.
- `POST /recipes/{rid}/stock` (servings) books each position's amount × servings / base servings on the position's
  product, default place and best-before (spec call 6); packets become their measured amount, pieces-of-packets
  products 1 per packet (spec call 7); redirects to the recipe with "Added 6 ingredients." and an Undo form posting the
  transaction ids to `POST /recipes/{rid}/stock/undo` (uses `grocy.undo`, reports any that can't be undone).

- [ ] Tests first: `test_add_ingredients_confirm_lists_and_skips`; `test_add_ingredients_books_scaled_amounts` (Review
  Focus 5); `test_add_ingredients_undo`; `test_add_ingredients_never_on_save` (import Save adds no stock).
- [ ] Implement (frontend-design). Full suite green. Commit: `feat: add a recipe's ingredients to inventory`.

### Task 8: Cook from what we have

**Files:** `app/suggest.py`, `app/ai.py` (WEEK_PROMPT variant), `app/web.py` (plan route), `app/templates/plan.html`, tests.

**Interfaces — produces:**
- `suggest.build_context(grocy, monday, servings: int | None = None)`: fulfillment per recipe counts an ingredient as
  stocked when stock covers its amount scaled to `servings` (the household's usual servings setting); positions with no
  stock amount are ignored (spec call 1); adds `"fully_stocked": bool`.
- `suggest.suggest(grocy, ai, monday, from_stock: bool = False)`: with `from_stock`, Claude gets "fill slots from fully
  stocked recipes first, then most stocked, then expiring stock; no repeats in the week; avoid recipes cooked in the last
  3 weeks unless nothing else is fully stocked"; free mode uses `local_picks(..., from_stock=True)` with that ranking.
- Plan: a "Cook from what we have" action next to Suggest (same review-before-adding flow as Suggest today).

- [ ] Tests first: `test_fulfillment_scales_to_servings_and_ignores_unknown_amounts`; `test_local_picks_from_stock_order`;
  `test_from_stock_prompt_flag`; `test_plan_has_cook_from_stock_action`.
- [ ] Implement; full suite green. Commit: `feat: suggest the week from what we have`.

## Order

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. Tasks 1–3 share the review page and importer; 4 underlies 5–7's displays; 6 and 4 both
touch Inventory's More. Never run two at once.
