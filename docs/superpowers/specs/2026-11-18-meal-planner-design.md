# Meal Planner — Design

Date: 2026-11-18
Status: approved by owner 2026-11-18

## Intent and why

A shared kitchen system for two people (the owner and their girlfriend), used mostly from phones. The owner has ADHD and forgets food they already have, so the system must *remember for them*: what's in the house, where it is, when it expires, what to cook this week, and what to buy. Recipes come from EveryPlate-style PDFs and recipe websites, and every recipe is displayed in the cookingforengineers.com tabular format (ingredients down the left, operations as merged cells to the right showing which ingredients combine at each step — see the brownie example the owner provided).

## Goals

1. **Single interface.** Both users only ever open one web app. No one uses Grocy's UI day to day. Devices: two **Android phones** (installed PWA via Chrome) and a **kitchen iPad** (Safari "Add to Home Screen" web clip). See Devices.
2. **Recipe import** from PDF (EveryPlate cards etc.) and from website URL, AI-assisted.
3. **Cooking for Engineers table** rendering for every recipe.
4. **Portion scaling** — pick servings, all quantities scale.
5. **Macros** per serving and per recipe (calories, protein, carbs, fat), scaling with portions.
6. **Inventory** — barcode scanning with the phone camera, storage location per item, quantity remaining.
7. **Automatic stock deduction** when a recipe is marked cooked; manual override (edit amount) anywhere.
8. **Expiration tracking** with visible "use soon" list and Home Assistant notifications.
9. **Weekly meal plan** with AI suggestions that favor items expiring soon and things on hand.
10. **Automatic shopping list** — anything a planned recipe needs that is missing or insufficient is added.
11. **Walmart hand-off** — one tap opens Walmart with the shopping list in the cart, for a pickup order.
12. **Home Assistant connection** — expiring items and shopping list visible in HA, notifications.

## Non-goals (for now)

- Tracking snacks / ad-hoc consumption outside recipes. (Manual override covers corrections.)
- Camera-based "Amazon Go" add/remove tracking. **Future.** Design must not block it: every stock change goes through Grocy's API, so a future camera service just calls the same endpoints.
- Placing the Walmart order automatically or checking out. The user always reviews and checks out on Walmart.
- Multi-household, public internet sign-up, user accounts beyond the two people.
- Native iOS/Android apps.

## Anti-goals (would be failure even if it "works")

- Shopping list silently wrong (missing items or phantom items) — trust is the whole point for an ADHD safety net.
- Two sources of truth for ingredients/stock that drift apart.
- Needing to open Grocy, Mealie, or a second app to complete normal tasks.
- Data loss of recipes or inventory.

## Constraints

- Hosted on the owner's **Proxmox** server.
- Owner already runs **Home Assistant**.
- AI tasks use **Claude** (Anthropic API). Gemini was offered as an alternative; Claude chosen for PDF/vision quality.
- Walmart has no cart-write API, but has an official public Add To Cart link service (see Walmart hand-off).
- Phone camera access in a browser requires HTTPS.
- Devices: Android phones only, plus one iPad in the kitchen.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| A | Grocy (inventory) + Mealie (recipes) + glue app | Two ingredient models to keep in sync; mapping drift silently breaks the shopping list. Rejected. |
| **B** | **Grocy as headless backend + one custom app as the only UI** | **Chosen.** Grocy already does stock, units, locations, expiry, recipe fulfillment, "add missing to shopping list", HA integration. Custom app supplies UI, AI import, CfE tables, macros, suggestions, Walmart. |
| C | Fully custom app | Single codebase, but rebuilds mature inventory/unit-conversion/expiry logic. ~3–4× the work. Rejected. |

Owner picked **B**, with the extra requirement that the custom app is the *only* interface (all Grocy interaction via its REST API).

## Architecture

```
Phone (PWA, HTTPS) ──► Meal Planner app (FastAPI) ──► Grocy REST API ──► Grocy SQLite
                              │                              ▲
                              ├──► Claude API (import, CfE structure, suggestions)
                              ├──► Open Food Facts / USDA FDC (nutrition)
                              └──► Walmart (deep link only, opened by phone)
Home Assistant ──► Grocy integration (sensors: expiring, missing, shopping list) ──► notifications
```

- **Proxmox:** one Debian LXC running Docker Compose with two containers: `grocy` (linuxserver/grocy) and `mealplanner` (our app). Grocy is not exposed outside the LXC except to HA.
- **Grocy is the single source of truth** for products, stock, locations, units, recipes, recipe ingredients, meal plan, and shopping list.
- **Extra data lives in Grocy userfields**, so there is no second database:
  - Recipe userfields: `cfe_table` (JSON describing the table structure), `source_url`, `source_pdf` (file stored via Grocy files API).
  - Product userfields: `walmart_item_id`, `staple` (bool), nutrition per 100 g/ml (`kcal`, `protein_g`, `carbs_g`, `fat_g`). Required: Grocy only has a single `calories` field, and its Open Food Facts lookup plugin fetches name/image only, so the app calls Open Food Facts itself for nutrition.

## Components (custom app)

1. **Importer**
   - URL: fetch page, extract schema.org `Recipe` JSON-LD when present (most recipe sites have it); fall back to sending page text to Claude.
   - PDF: send PDF to Claude (native PDF input) and get structured JSON back.
   - Claude output schema: title, servings, ingredients (name, qty, unit, note), steps, and the **CfE operation tree**.
   - **Ingredient matching:** each ingredient is matched to an existing Grocy product (fuzzy name match + Claude assist). Unmatched → user confirms "create new product" or picks one. This review screen is mandatory before save — it's what keeps the shopping list honest.
   - Saves recipe + recipe ingredients (`recipes_pos`) into Grocy.

2. **CfE table renderer**
   - Data model: ordered ingredient rows + list of operations, each operation `{label, covers: [row indices or prior operation ids]}`. Rendered as an HTML `<table>` with `rowspan`/`colspan`, reproducing the example image (top rows for pre-steps like "Preheat oven"; final column for the last operation).
   - Claude generates the tree at import; user can edit it (simple form: operation label + which rows it covers).
   - Non-tree recipes (an ingredient split across two steps): the ingredient row is repeated with its partial amount (builder's call), matching how cookingforengineers handles it. If no sensible table exists, fall back to numbered steps beside the ingredient list.
   - **Unit conversions (builder's call):** the import review screen proposes product-specific conversions ("1 cup flour = 125 g"), Claude suggests, user confirms; stored as Grocy quantity-unit conversions so fulfillment, shopping list and macros all agree.
   - Quantities shown come from the scaled ingredient list.

3. **Scaling and macros**
   - Servings selector scales quantities (Grocy recipes have base servings; the app multiplies).
   - Macros = Σ(ingredient qty converted to g/ml × product nutrition per 100) ÷ servings. Nutrition source: Open Food Facts (by barcode, automatic on scan) else USDA FoodData Central search (on product creation).
   - Products with unknown nutrition are flagged on the recipe ("macros incomplete: 2 ingredients missing data").

4. **Inventory**
   - Scan screen: camera barcode scan. Android Chrome has a native `BarcodeDetector` but iPad Safari does not, so use one ZXing-based JS decoder on all devices (one code path). Lookup in Grocy by barcode; unknown → Grocy's Open Food Facts lookup / create product.
   - Actions after scan: **Add** (qty, location, best-before date — defaulted from product's default days), **Use** (consume qty), **Open**, **Move location**.
   - Pantry list grouped by location, sorted by expiry. "Use soon" section at top.
   - Manual edit of any amount (Grocy inventory correction).

5. **Meal plan and shopping list**
   - Week view (Grocy meal plan). Add recipes manually or accept AI suggestions.
   - **The household's week** (owner-provided): Mon–Fri dinners, where each dinner's leftovers are the next day's lunch; Sat and Sun both get a cooked lunch and dinner. Two people.
     - Default servings per slot (editable per entry): Sun–Thu dinner = **4** (2 dinner + 2 next-day lunch); Fri dinner = 2; Sat dinner = 2; Sat lunch = 2; Sun lunch = 2. Mon lunch comes from Sun dinner. (Builder's call on how Sun dinner / Fri dinner leftovers fall; owner can adjust the defaults.)
     - Lunches that are leftovers show in the week view as "Leftovers: <recipe>" and don't add to the shopping list.
     - Shopping window = the plan week Mon–Sun; shopping day is a setting (default Sunday).
   - **Do not call Grocy's per-recipe "add missing to shopping list".** Verified in Grocy source (`RecipesService::AddNotFulfilledProductsToShoppingList`): each recipe is checked against full stock independently, so two recipes sharing an ingredient under-add, and Grocy never removes items when a recipe is unplanned.
   - Instead (builder's call): the app owns a dedicated Grocy shopping list named **"Meal plan"**. On every plan change it **rebuilds that list from scratch**: sum each product's need across all planned meals in the shopping window (scaled by each entry's servings, using Grocy's `mealplan-week`/shadow recipe fulfillment data or the app's own sum over `recipes_pos`), subtract stock in hand, write the shortfall. Manual items go on a separate **"Extras"** list that the app never touches. Both lists show together on one screen.
   - **Generic products:** recipes reference generic products ("All-purpose flour"); scanned brand items are Grocy *child products* of the generic parent, so stock rolls up. Owner wants this **as automated as possible**, so a new barcode is resolved with no questions in the normal case:
     1. Look up Open Food Facts (name, brand, categories, package size e.g. "2.27 kg", nutrition). Fallback: UPCitemdb free tier (name/brand only).
     2. Claude maps the result to an existing generic parent product (or creates one), sets package size, stock unit, default location (e.g. dairy → Fridge) and default best-before days by category.
     3. Only if no database knows the barcode: user snaps a photo of the package, Claude reads it (vision) and proceeds as above.
     4. A "just added" toast lets the user fix a wrong guess with one tap; the mapping is then remembered for that barcode.
   - **Staples (builder's call):** products flagged "staple" (salt, oil, spices) are skipped in the shortfall calc unless their stock is zero.
   - "Cooked it" button on a planned meal → consume via that meal plan entry's shadow recipe (so the entry's servings apply). Note: Grocy consume never fails on shortfall; the app shows "you were short on X — correct stock?" when fulfillment said something was missing.
   - Shopping list screen: check off items; "Bought" moves them into stock with best-before defaulted from the product's default days (editable, never blocking).

6. **Suggestions**
   - Send Claude: recipe library summary, current stock with expiry dates, last N weeks of plan (avoid repeats). Ask for a week of meals prioritizing expiring items and high fulfillment. Show with reasons ("uses spinach expiring Thu"). User accepts per day.
   - Suggestions from the existing library only (v1). New-recipe discovery is out of scope.

7. **Walmart hand-off**
   - **Official Walmart Add To Cart service** (owner found docs: https://walmart.io/docs/atc/v1/add-to-cart). No affiliate account or API key needed:
     `https://www.walmart.com/sc/cart/addToCart?items=945193065,660768274_2&storeId=5435`
     - `items`: comma-separated Walmart **item IDs**, `_qty` suffix (pipe `|` also accepted; omit when qty is 1).
     - `storeId`: optional fulfillment store → set once in app settings to the owner's pickup store.
     - If any item fails to add, Walmart shows an error modal; the rest still land in the cart.
   - Each product holds a `walmart_item_id`. Set once per product: the app opens a Walmart search for the product name; the user pastes the product page link and the app pulls the ID out of the URL (`walmart.com/ip/<slug>/<itemId>`). Remembered forever after.
   - Button builds the link for all list items that have an ID; items without an ID show as a "still to add" list with search links.
   - Quantity: shopping-list shortfall is converted to whole packages of that product (e.g. need 300 g flour, package is 2.27 kg → 1).
   - Quick check at build time: whether Android opens this link in the Walmart app or browser, and that the cart carries over either way.

## Home Assistant

- Add the Grocy integration (built into HA core; needs a Grocy API key and the Grocy port reachable from HA) pointed at the Grocy container: sensors for expiring/expired/missing products and shopping list.
- HA automation: daily notification to both phones listing items expiring within N days.
- Optional: HA dashboard card linking to the app.

## Devices

- **Android phones (both users):** primary device for scanning, shopping list, planning. Installed as a PWA from Chrome. HA notifications via the HA Android companion app.
- **Kitchen iPad (owner-provided):** Safari web clip pinned to the home screen. Needs `apple-touch-icon` + `apple-mobile-web-app-capable` meta so it opens full-screen. Main use is cooking: the CfE table view gets a landscape tablet layout with large text.
- **Keep screen on while cooking (builder's call):** recipe view requests a Screen Wake Lock (supported in Safari / home-screen web apps on current iPadOS and in Chrome), so the iPad doesn't sleep mid-recipe.
- The iPad needs the Tailscale app installed and signed in to the same tailnet.

## Access and auth

- **Tailscale** (both phones already have it). Tailscale on the LXC with `tailscale serve` provides HTTPS (needed for the camera) and access from anywhere with no open ports. No in-app login; the tailnet is the boundary.

## Starting state

- Nothing is running yet: fresh Grocy install, no existing inventory data.
- **Bulk recipe import is in v1** (owner has a backlog). Format and size TBD. Planned shape: drop many files/URLs at once → each runs through the normal importer in a background queue → recipes land in a "Needs review" list, where the user confirms ingredient matches one recipe at a time. Nothing is saved to Grocy unreviewed, so the shopping list stays trustworthy.

## Error handling that matters

- Claude import failure or low confidence → keep the raw source attached, show what was parsed, let the user fix; never save half a recipe silently.
- Grocy unreachable → app shows a clear error; no local caching of writes (no drift).
- Backups: Proxmox scheduled backup of the LXC (covers Grocy DB and uploaded PDFs).

## Tech choices (builder's call)

- Python 3.12, FastAPI, Jinja2 server-rendered pages + small vanilla JS; PWA manifest for home-screen install.
- `httpx` for Grocy/Claude/nutrition APIs; `anthropic` SDK.
- No app database — Grocy holds everything. One exception (builder's call, needed by the bulk-import queue): unreviewed imports are kept as JSON files (plus the source PDF) in a Docker volume folder until the user confirms them, then written to Grocy and deleted. Covered by the LXC backup.

## Left to the builder

- Exact Grocy endpoints/userfield setup, barcode library choice, CfE edit UI details, prompt wording, unit conversion edge cases (lean on Grocy's quantity unit conversions).
