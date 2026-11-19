# Recipe photos, table-first recipe page, plain-language table editor

Owner requests (2026-11-18): "I want the photo for the dish to be visible. When I go to view the recipe, I want the
recipe table to be the primary thing I see. Reviewing the import, why are there prefixes to the ingredients?? This
doesn't make sense..."

Global rules: the house-manager plan's Global Constraints (Midnight tokens, no inline styles, 48px targets, sentence
case, no middle dots/arrows in text, no overlap from 320px); load `frontend-design:frontend-design` before UI work;
TDD; existing tests are the behavioural contract.

## Task A: Table editor without internal ids

**Files:** `app/templates/_cfe_editor.html`, `app/templates/review.html`, `app/templates/table.html`, `app/web.py`
(form parsing for the review and table routes), `app/static/style.css`, `app/static/app.js` (only if needed),
`tests/test_web.py`.

- The CfE model keeps its ids (`i0`…, `o1`…) internally; the UI never shows or asks for them.
- Review ingredient rows: no `i17` prefix — just the ingredient text.
- Steps editor: each step row = a "What to do" text field + **Combines** as a wrapping set of toggle chips
  (checkboxes styled as chips, min 44–48px tall): every ingredient by name, plus every earlier step by its label
  (e.g. "boil, cover, simmer 15–20 min"). Ingredients whose names repeat show their amount to tell them apart
  ("kosher salt, 1 tsp" / "kosher salt, to taste"). A step can't combine itself or later steps. Step order is the
  row order; "Add step" adds an empty row at the end; a way to remove a step (button "Remove step").
- Hint copy: "Each step combines ingredients and earlier steps. Tap what goes into it."
- Server: parse the chip checkboxes back into the same `op_inputs` id lists the table model already uses (ids come
  from the checkbox values; never trust them blindly — reject unknown ids with the existing validation messages).
  Auto-assign step ids (`o1`, `o2`, … in row order, remapping references when a step is removed).
- Tests: `test_review_shows_no_internal_ids` (no `>i0<`/`i17` style labels or "o1, i2" placeholders in review/table
  pages), `test_combines_chips_round_trip` (post chips → saved table has the right inputs), `test_duplicate_names_show_amounts`,
  `test_remove_step_remaps_references`, `test_unknown_input_id_rejected`.
- Commit: `feat: table editor picks ingredients and steps by name`.

## Task B: Dish photos and a table-first recipe page

**Files:** `pyproject.toml`/`uv.lock` (add `pypdf` and `pillow`), `app/importer.py`, `app/grocy.py`, `tests/fakes.py`,
`app/web.py`, `app/templates/recipe.html`, `recipes.html`, `review.html`, `app/static/style.css`, `tests/…`.

- **Find the photo at import:**
  - URL imports: recipe JSON-LD `image` (string, list, or ImageObject `url`), else `og:image`; download with the
    app's httpx client (image/* only, max 5 MB, follow redirects, timeout); failures are silent (no photo).
  - PDF imports: the largest embedded raster image on the first two pages via `pypdf` (by pixel area, ignoring
    images smaller than 200×200), re-encoded to JPEG with Pillow (max 1600px long edge, quality 85).
  - Keep the photo with the pending import (next to the PDF in `PendingStore`, `<id>.jpg`).
- **Store in Grocy on save:** Grocy recipe pictures — `PUT /api/files/recipepictures/{base64(filename)}` with the
  bytes (`Content-Type: application/octet-stream`), then set the recipe's `picture_file_name`. Add
  `Grocy.put_recipe_picture(filename, data)` and `Grocy.recipe_picture(filename, width) -> bytes` (`GET
  /api/files/recipepictures/{base64}?force_serve_as=picture&best_fit_width=<w>`); FakeGrocy equivalents.
- **Serve:** `GET /recipes/{id}/photo?w=…` proxies Grocy (w clamped to 200..1600), `Cache-Control: max-age=86400`;
  404 when the recipe has no picture. `POST /recipes/{id}/photo` (file upload, image/* only, 10 MB cap,
  re-encoded with Pillow) adds or replaces the photo — an "Add a photo" / "Change photo" control on the recipe page.
- **Show:**
  - Recipes list: photo cards (2 columns on phones, more on wide), name under the photo; recipes without a photo get
    a quiet placeholder tile (icon), not a broken image. `loading="lazy"`, `alt` = recipe name.
  - Review screen: the found photo at the top (or "No photo found" + the add control after saving).
  - Recipe page — **the table is the primary thing**: title row = small rounded photo thumbnail (about 64px; tap
    opens the full photo) + recipe name; directly under it a compact servings stepper on one line, then the
    Cooking for Engineers table immediately. Everything else (Steps, Per serving macros, Add to plan, Edit table,
    Open the source, photo add/change) comes after the table.
- Tests: JSON-LD image (string/list/object) and og:image extraction; PDF largest-image extraction (fixture PDF built
  in the test with pypdf + Pillow); upload validation (non-image 400, too big 413); photo route 404 without picture;
  recipe page order (table before "Per serving" and "Add to plan"); recipes list shows `<img` for a recipe with a
  picture and the placeholder for one without.
- Commits: `feat: dish photos from imports, stored in Grocy`; `feat: recipe page leads with the table`.

## Task E: Cooking aids on the recipe table (owner, 2026-11-18)

Owner: "I love the option in recipetables.com to be able to click on a step to cross it out. That will help me know
what else I have left to do. I am still just a home chef, so it would be nice if it told me WHERE to combine things:
if it is in a separate bowl / pan."

- **Where:** `cfe.Op` gains `where: str = ""` (short: "in a large pot", "in a small bowl", "in the skillet",
  "on a sheet pan"). The AI table prompt asks for it on every step: the vessel the recipe names, else a sensible
  home-kitchen choice, never blank for steps that combine things; a step that continues in the same vessel as its
  input says so ("same pot"). Shown in the table's step cell as a second, quieter line under the label. The table
  editor (Task A's step rows) gets a "Where" text field per step. Old tables without `where` render as before.
- **Cross out as you cook:** tapping a step cell or an ingredient cell on the recipe page toggles it done
  (strike-through + dimmed, `aria-pressed`), keyboard-operable (cells are buttons or have role/tabindex + Enter/
  Space). State is kept per device in localStorage, keyed by recipe id (wrapped in try/catch; works without
  storage), and a "Start over" button clears it. Crossing out a step does not cross out its ingredients
  automatically (people tick ingredients as they prep). Respects reduced motion.
- Tests: Op round-trips `where`; the prompt mentions where/vessel; the table renders the where line; the editor
  saves a where value; the recipe page has the cross-out hooks (data attributes/roles) and a Start over control.
- Commit: `feat: where each step happens, and cross steps off while cooking`.
- Order: after Task A (editor rows) and together with Task B's recipe-page rework (same template).

## Task F: Monthly AI budget with full, economy and free modes (owner, 2026-11-18)

Owner: "I get $100 of credits for the API usage every month... Only do the cost optimizations once at $80, then only
go to free once all $100 have been utilized. Keep full functionality before then."

- **Meter**: every Claude call records `response.usage` (input, cache writes 5m/1h, cache reads, output; model;
  call type) in the HouseStore with its cost at list prices (a small price table in app/ai.py: Opus 5.5 $4/$20,
  cache read $0.20, writes 1.25x/2x; Sonnet 5.5 $2/$10, read $0.10; Haiku 5.5 $0.10/$0.50, read $0.01, writes
  1.25x/2x; batch = 0.5x of all; source: the Pricing page fetched 2026-11-18 — keep it in one place). Month =
  calendar month, local time zone. Settings `AI_MONTHLY_BUDGET` (default 100) and `AI_ECONOMY_AT` (default 80).
- **Modes** from this month's spend: **full** (< economy_at): exactly today's behaviour; **economy** (economy_at ≤
  spend < budget): recipe structuring at effort `medium` (same model), product guesses / conversions / ingredient
  matching on `claude-haiku-5-5` (effort low), bulk imports of 3+ files through the Message Batches API (50% off;
  imports show "Reading — usually a few minutes" until results arrive; poll in the reminder loop or a small worker;
  results land in the pending store like normal imports), matching sends only the ~30 most likely products per
  ingredient list (local fuzzy prefilter), web pages without JSON-LD are trimmed to the recipe text and capped
  (~20k characters); **free** (≥ budget): no Claude calls at all.
- **Free-mode fallbacks**: website imports with JSON-LD save ingredients + steps without a CfE table (the recipe page
  already has a no-table fallback; the table can be filled in later with "Make the table" when AI is back); PDF and
  photo imports are queued ("Waiting for next month's AI budget — it will be read on December 1") and processed
  automatically when the month rolls over; barcode scans use Open Food Facts / UPCitemdb only, and an unknown item
  opens a short manual product form (name, generic, unit, location, package size, food/supply); ingredient matching
  uses plain normalised name matching; unit conversions ask the reviewer for the factor; week suggestions use a
  local heuristic (expiring stock first, not cooked in the last 3 weeks, fill open slots).
- **Always on** (no behaviour change): the meter, and prompt caching of the system prompt + schema
  (`cache_control` on the system block) — it only makes repeated instructions cheaper.
- **Me page**: "AI this month: $83.20 of $100 — economy mode" (plain sentence, no middle dot in the final copy),
  with a one-line explanation of the current mode. Today shows a quiet note only in free mode.
- Every AIError path keeps working; mode switches never crash a page.
- Tests: cost math for each meter field and model; mode thresholds and month rollover; full mode sends exactly the
  current requests (model/effort unchanged); economy picks the cheaper settings per call type; free mode makes no
  Claude call for each feature and uses its fallback; batch submission/collection with a fake batches API; queued
  PDFs processed after rollover.
- Order: after Tasks B+E (both touch app/importer.py and app/ai.py), before C and D.
- Commits: `feat: monthly AI meter and budget modes`; `feat: economy mode settings and batch imports`;
  `feat: AI-free fallbacks once the month's budget is used`.
