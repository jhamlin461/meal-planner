# Inventory tab, scan-and-confirm scanner, Settings and themes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tabs become Today, Meals, Inventory, Chores; one place-grouped Inventory with both kinds of delete and
editable places; Me becomes a Settings button with Midnight/Black/Light/Match phone themes; the scanner works like
Sam's Club Scan & Go (stop on read, confirm in a bottom sheet, undo from this trip).

**Architecture:** Inventory logic (rows, places, clear, remove) goes in a new `app/inventory.py` of plain functions
over the Grocy client, with thin routes in `app/web.py`. Places become live Grocy data everywhere (no fixed list in
checks). The scanner splits today's `/api/scan` into lookup, confirm and undo so nothing changes before the person
confirms; the page is rebuilt as a full-screen camera with a bottom sheet in vanilla JS.

**Tech Stack:** FastAPI, Jinja2, vanilla JS, Grocy REST (`/stock/transactions/{id}/undo` for undo), pytest with
FakeGrocy.

**Spec:** `docs/superpowers/specs/2026-11-18-inventory-tabs-scan-themes-design.md` (read it; its "Calls made"
section is binding).

## Global Constraints

- Load `frontend-design:frontend-design` before any UI work. Keep Midnight tokens verbatim: background `#383C45`,
  surface `#434954`, rail `#2E333A`, action `#5294E2`, needs-you-now `#E45E65`, due-soon `#F4B400`, text `#FFFFFF`,
  muted `rgba(255,255,255,.68)`. Midnight is the default.
- No new runtime dependencies. No external CSS/JS/font requests. Icons from the existing self-hosted MDI sprite
  (`_icon.html`); add symbols to the sprite if needed.
- No inline styles (tests check this). Controls never overlap at any width ≥ 320 px, no horizontal page scroll;
  touch targets ≥ 48 px; sentence case, no all-caps; no middle dots or arrows in UI text; buttons name their action.
- Everything keeps working without JS where it works today (forms post and redirect); the scanner needs JS (camera).
- Existing tests keep passing except where this plan moves a route or page on purpose; update those tests to the new
  route, never delete coverage. `create_app` gains only keyword-only parameters with defaults.
- Grocy field names must be Grocy's real ones (FakeGrocy rejects unknown chore columns; check Grocy's migrations
  when adding new writes).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A place renamed or removed in the app, then the app restarts: it must not come back or be duplicated.
2. Remove item on a product a recipe or a "uses up a supply" chore depends on: refused with the names, nothing deleted.
3. Two quick scans of different codes: the second must not start a lookup while the first sheet is open.
4. Undo after the stock was already used (Grocy refuses): the trip says so; nothing else breaks.
5. Black theme: every surface that was a lighter panel is #000 with an outline; text and the action colour stay
   readable (AA) and the Today warning colours still show.

---

### Task 1: Places are live Grocy data, with Manage places

**Files:** Create `app/inventory.py`; modify `app/setup_grocy.py`, `app/barcode.py`, `app/importer.py`,
`app/web.py`, `app/ai.py` (only if its prompt lists places), templates that list places (`scan.html`,
`review.html`, `pantry.html` Move select); create `app/templates/places.html`; tests `tests/test_inventory.py`,
`tests/test_setup_grocy.py`, existing tests touched by the checks.

**Interfaces — produces:**
- `inventory.places(grocy) -> dict[str, int]`: name → id, in Grocy's order by name with `LOCATIONS` order first for
  the starting names (so Fridge, Freezer, Pantry… stay first, new places after, alphabetical).
- `inventory.default_place(grocy) -> str`: "Pantry" if it exists, else the first place.
- `inventory.add_place(grocy, name: str) -> int` (ValueError "There's already a place called X." / "Give the place
  a name."), `rename_place(grocy, id, name)` (same errors), `remove_place(grocy, id, move_to: int)` (ValueError
  "Move or use up what's in X first." when any stock entry is there; re-points every product whose `location_id` is
  `id` to `move_to`, then deletes the location).
- Routes: `GET /inventory/places`, `POST /inventory/places` (name), `POST /inventory/places/{id}/rename` (name),
  `POST /inventory/places/{id}/remove` (move_to). Errors re-render the page with the message (`role="alert"`).

- [ ] Write failing tests: `test_bootstrap_seeds_places_only_on_empty_grocy` (a Grocy with locations
  ["Kitchen shelf"] gets no starting places added; an empty one gets all of `LOCATIONS`);
  `test_add_rename_remove_place` (duplicate and blank names refused; remove with stock refused with the message;
  remove re-points a zero-stock product's default place); `test_places_order_starting_first`;
  `test_scan_manual_accepts_a_new_place` (manual product with location "Basement" after adding it succeeds; an
  unknown place is refused); `test_review_manual_product_accepts_new_place`; `test_manage_places_page` (form posts
  round-trip; no inline styles).
- [ ] Run them: they fail.
- [ ] Implement: in `bootstrap`, create `LOCATIONS` only when `grocy.objects("locations")` is empty; replace every
  `in LOCATIONS` check and the `"Pantry"` fallback (`barcode.py:108`, `web.py` manual/review checks) with
  `inventory.places(grocy)` / `default_place(grocy)`; pass `list(places(grocy))` where Claude gets the location
  choices; templates take `locations` from `places(grocy)`.
- [ ] Run the full suite: green.
- [ ] Commit: `feat: places are editable, and every place check uses Grocy's own list`.

### Task 2: The Inventory tab (one list by place) replaces Pantry and Supplies

**Files:** `app/inventory.py`, `app/web.py`, `app/templates/base.html`, create `app/templates/inventory.html`,
delete `pantry.html` and `supplies.html` once unused, `app/static/style.css`, `app/static/app.js` (only if needed),
tests (`tests/test_inventory.py`, move Pantry/Supplies tests in `tests/test_web.py`/`tests/test_home.py` to
`/inventory`).

**Interfaces — consumes:** Task 1's `places`. **Produces:**
- `inventory.rows(grocy, settings, today: date) -> Inventory` where `Inventory` has `soon: list[dict]` (food only,
  today's Pantry "Use soon" logic moved here), `groups: dict[str, list[dict]]` (place name → rows, places in
  `places()` order, empty places omitted), `totals: dict[int, float]`. A row dict: `pid, loc, name, amount, unit,
  kind ("food" | "supply"), when, past` and for supplies `min`, `total` (all places) and `low`. Rules (pre-flight
  ruling, replaces spec "Calls made" 2 for supplies): food = one row per product per place with stock > 0; supply =
  one row per generic per place where it or any of its brands has stock (the row's amount is that place's share;
  Running low uses the generic's overall total), and a supply with no stock anywhere shows once under its default
  place at 0.
- Supply rows' More: Move to (transfers each of the generic's products' stock in that place) and Keep at least; no
  Set total (Used one, scanning and Clear stock cover supplies). Food rows' More: Set total and Move to as today.
- `GET /inventory?place=<id>`: chips "All" + each place that has rows (`aria-current` on the active one, plain
  links so no JS is needed); the Scan title action; Use soon; groups. Food rows: Used [amount] Use (posts to the
  existing consume route). Supply rows: "Used one", running-low wording as today. Every row: a `More` disclosure with
  Set total, Move to (food rows; supplies move their row's amount from that place), Keep at least (supplies).
  Task 3 adds Clear stock and Remove item into the same disclosure.
- Existing POST routes keep their paths but redirect back to `/inventory` (keep `place` when given):
  `/pantry/{pid}/consume|amount|move`, `/home/supplies/{pid}/used|min`.
- Redirects (303): `GET /pantry` → `/inventory`, `GET /home/supplies` → `/inventory` (carry `msg`).
- `base.html`: tabs Today `/`, Meals `/plan`, Inventory `/inventory`, Chores `/home`, Me `/me` (Me stays until Task
  4). Meals pages: Plan, Recipes, Shopping. Chores has no page switcher. Scan's title action appears only on
  `/inventory`; `/scan` belongs to the Inventory section (drop `?from=chores`; keep accepting it).
- Empty state: "Nothing here yet. Scan something to add it." with a Scan link.

- [ ] Write failing tests: `test_inventory_groups_food_and_supplies_by_place` (fridge yogurt and closet paper
  towels in their groups; a zero-stock supply still shows; zero-stock food doesn't); `test_supply_row_under_place_with_most_stock`;
  `test_inventory_place_chip_filters` (`?place=` shows one group; chips only for places with rows);
  `test_old_pantry_and_supplies_urls_redirect`; `test_inventory_forms_return_to_inventory` (consume, set total,
  move, used one, keep at least each redirect to `/inventory`); `test_nav_tabs` (four section tabs plus Me; Meals
  pages Plan/Recipes/Shopping; no Supplies/Pantry links); `test_inventory_no_inline_styles_and_scan_action`.
- [ ] Run them: they fail. Implement (load the frontend-design skill first; chips are a horizontally wrapping row,
  never a horizontal scroller at 320 px). Move Pantry/Supplies tests to the new URLs.
- [ ] Full suite green; check 390 px and 1440 px in the demo server (`uv run python -m tests.demo_server <port>`).
- [ ] Commit: `feat: one Inventory tab, grouped by place, replaces Pantry and Supplies`.

### Task 3: Clear stock and Remove item

**Files:** `app/inventory.py`, `app/web.py`, `app/templates/inventory.html`, `app/static/app.js` (confirm without
a dialog element is fine: a `<form>` with `onsubmit="return confirm(...)"` matches the existing Discard pattern),
`tests/fakes.py` (if FakeGrocy needs delete for related rows), `tests/test_inventory.py`.

**Interfaces — produces:**
- `inventory.users_of(grocy, pid: int) -> list[str]`: names of recipes with a `recipes_pos.product_id == pid` and of
  chores with `product_id == pid` and `consume_product_on_execution == 1` (sorted, unique).
- `inventory.clear_stock(grocy, pid)`: stock to 0 across places (Grocy inventory to 0); the product stays.
- `inventory.remove_product(grocy, pid)`: ValueError("Fudgy brownies and Change the litter use this. Change them
  first, or use Clear stock.") when `users_of` is not empty (one name: "Fudgy brownies uses this."); otherwise zero
  its stock, delete its `product_barcodes` and `shopping_list` rows, then the product. A brand never checks its
  generic's users. A generic (supply rows are generics) is removed together with its brands: the users check covers
  the generic and every brand, and each brand is removed the same way first (pre-flight ruling: the person sees one
  item, so they must be able to remove it).
- `inventory.clear_stock` on a generic zeroes the generic and every brand.
- `POST /inventory/{pid}/clear`, `POST /inventory/{pid}/remove` → back to `/inventory` with `msg` ("Cleared Greek
  yogurt." / "Removed Greek yogurt."), or the refusal shown as the message (`role="alert"`, not a 5xx).
- In the More disclosure: Clear stock (with the spec's one-line explanation) and Remove item (confirm text: "Remove
  Greek yogurt? Its stock, barcode and details are deleted. This can't be undone.").

- [ ] Write failing tests: `test_clear_stock_keeps_product`; `test_remove_product_deletes_product_barcodes_and_list_rows`;
  `test_remove_refused_when_recipe_or_chore_uses_it` (message names both; nothing deleted);
  `test_remove_brand_ignores_generic_users`; `test_remove_generic_with_brands_refused`;
  `test_inventory_more_has_clear_and_remove`.
- [ ] Run, implement, full suite green.
- [ ] Commit: `feat: clear an item's stock, or remove the item`.

### Task 4: Settings button replaces Me; Black and Match phone themes

**Files:** `app/web.py`, `app/templates/base.html`, rename `me.html` → `settings.html`, `app/static/style.css`,
tests in `tests/test_web.py`.

**Interfaces — produces:**
- `GET /settings` (the old Me rows: This device, Theme, AI this month, People); `GET /me` → 303 `/settings`.
  `/who` and `/home/people` belong to the Settings section (no tab highlighted; Settings button marked current).
- Tabs: Today, Meals, Inventory, Chores (no Me). A Settings button (cog icon, `aria-label="Settings"`, 48 px) at the
  right of every page's title row; on the rail (≥900 px) a Settings link pinned to the bottom of the rail.
- `POST /settings/theme` accepts `dark | black | light | auto`; an unknown or missing cookie means `dark`.
  `html[data-theme]` gets the value. Settings → Theme is a four-option segmented control: Midnight, Black, Light,
  Match phone.
- CSS tokens: `[data-theme="black"]` sets `--bg: #000; --surface: #000; --rail: #000;` cards/rows/fields get a 1px
  `var(--line)` outline instead of relying on a fill; `--line` light enough to see on black; `color-scheme: dark`.
  `[data-theme="auto"]` uses the light tokens under `@media (prefers-color-scheme: light)` and the black tokens
  otherwise (no JS). `<meta name="theme-color">`: `#000000` for black, light value for light, `#2E333A` for dark;
  for auto two metas with `media="(prefers-color-scheme: …)"`.

- [ ] Write failing tests: `test_me_redirects_to_settings`; `test_settings_page_rows`; `test_tabs_have_no_me`;
  `test_settings_button_on_every_section` (`/`, `/plan`, `/inventory`, `/home` contain `href="/settings"` with
  `aria-label="Settings"`); `test_theme_values` (each of the four sets the cookie and `data-theme`; `bogus` → 422;
  no cookie → `dark`); `test_black_theme_meta_color`.
- [ ] Run, implement (frontend-design skill; check Black at 390 px for contrast of muted text, warnings and the
  action colour), full suite green.
- [ ] Commit: `feat: Settings replaces Me, with Black and Match phone themes`.

### Task 5: Scan API: lookup, confirm, undo

**Files:** `app/web.py`, `app/grocy.py`, `tests/fakes.py`, `tests/test_web.py` (scan tests) or new
`tests/test_scan_api.py`.

**Interfaces — produces (JSON, all POST):**
- `/api/scan/lookup` `{code}` → `{"status": "found", product_id, product, generic, unit, package (number|null),
  place (name), places (list of names), kind ("food"|"supply"), in_stock (number), photo (url|null), created (bool),
  need_min (bool: a new supply whose generic has no keep-at-least)}`; or today's `{"status": "unknown"}` /
  `{"status": "manual", code, name}` / `{"status": "error", message}`. Nothing in stock changes. `/api/scan/photo`
  and `/api/scan/manual` return the same `found` shape instead of adding stock.
- `/api/scan/confirm` `{product_id, action: "add"|"use", packs: number > 0 (default 1), amount: number|null (stock
  units, used when the package size is unknown), place: name|null (add only), min: number|null}` → `{"status":
  "ok", action, product, amount, place, transaction_id}`. Add: `packs * package` (or `amount`), into `place`
  (validated against `places()`; the product's default place is not changed), best-before as today; remembers an
  unknown package size as today; `min` sets keep-at-least as today's need_min. Use: supplies one unit per pack;
  never more than in stock; `amount` 0 → `{"status": "ok", amount: 0, transaction_id: null}`.
- `/api/scan/undo` `{transaction_id}` → `{"status": "ok"}` or `{"status": "error", "message": "Can't undo: already
  used."}` when Grocy refuses.
- `Grocy.add_stock` and `Grocy.consume` return the transaction id from Grocy's response (the list of stock log rows;
  take `transaction_id` of the first); add `Grocy.undo(transaction_id)` → `POST /stock/transactions/{id}/undo`.
  FakeGrocy mirrors all three (undo refuses with GrocyError 400 if the booked stock was consumed since).
- `/api/scan` (old one-step) and `/api/relink` keep working for now (the page stops using `/api/scan` in Task 6;
  remove it there if nothing else calls it).

- [ ] Write failing tests: `test_lookup_changes_no_stock`; `test_confirm_add_packs_into_chosen_place_keeps_default`;
  `test_confirm_add_unknown_package_uses_amount_and_remembers_it`; `test_confirm_use_supply_one_per_pack`;
  `test_confirm_use_never_more_than_stock`; `test_confirm_rejects_unknown_place`; `test_undo_reverses_add`;
  `test_undo_after_use_reports_already_used`; `test_lookup_new_supply_flags_need_min`;
  `test_manual_and_photo_return_found_without_adding`.
- [ ] Run, implement, full suite green.
- [ ] Commit: `feat: scan looks up first; confirm and undo change stock`.

### Task 6: The Scan & Go scanner page

**Files:** `app/templates/scan.html`, `app/static/app.js` (scanner section), `app/static/style.css`,
`app/templates/base.html` (the scan page hides the tab bar and rail: full screen), tests in `tests/test_web.py`.

**Interfaces — consumes:** Task 5's API, existing zxing vendor script.

- Layout per the spec sketch: full-screen camera with a scan frame; ✕ (`aria-label="Close scanner"`, back to
  `/inventory`); trip counter button ("3 added", "2 used", "1 added, 1 used"; hidden at 0); Add to stock / Use up
  toggle at the bottom.
- On a read: stop decoding (pause the reader; a `busy` flag alone is not enough), `navigator.vibrate?.(60)`, freeze
  on the frame, open the bottom sheet in a loading state ("Looking up…" with a shimmer; `prefers-reduced-motion`:
  text only). Then:
  - `found`: photo (if any), name, "Counts as {generic}" with a Change link (today's relink dialog), place select
    (Add only; default `place`), −/+ stepper (packs; min 1) or, when `package` is null, an amount field in `unit`;
    need_min adds "Keep at least" in the sheet; primary button "Add to {place}" / "Use 1" ("Use 2"…); Cancel.
  - `unknown`: "Take a photo of the package" (file input, capture) and "Fill in the details" (today's manual form,
    inside the sheet); both lead to the `found` state.
  - `error`: message plus Try again.
- Confirm → sheet closes, toast ("Added 2 Greek yogurt to Fridge" / "Used 1 paper towels"), trip gains a line,
  scanning resumes. Cancel/Esc/✕ on the sheet → resumes without changes.
- Trip sheet: list of this session's lines (newest first) with Undo each (calls undo; on error shows the message
  on that line; on success removes it and lowers the counter). Kept in `sessionStorage` (try/catch; works without).
- Camera errors keep today's message and the Open scanner in browser link. Focus moves into the sheet when it opens
  and back to the scanner when it closes; the sheet is a `<dialog>` (Esc closes).
- Sheet and stepper buttons ≥ 48 px; the sheet never covers the whole screen on phones (max ~70% height, scrolls
  inside).

- [ ] Write failing tests (template structure, since the JS needs a camera): `test_scan_page_full_screen_no_tabs`;
  `test_scan_page_has_sheet_stepper_trip_and_close`; `test_scan_page_no_inline_styles`.
- [ ] Implement (frontend-design skill). Check every sheet state in the demo server at 390 px and 1440 px by
  stubbing `fetch` from the browser console and opening the sheet with a code (no test hooks in the shipped JS).
- [ ] Full suite green.
- [ ] Commit: `feat: Scan & Go scanner: stop on read, confirm in a sheet, undo this trip`.

## Order

Task 1 → 2 → 3 → 4 → 5 → 6. Tasks 2, 4 and 6 all touch `base.html`; run them in this order, never in parallel.
