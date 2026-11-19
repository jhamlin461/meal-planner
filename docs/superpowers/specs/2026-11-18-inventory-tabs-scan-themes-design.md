# Inventory tab, scan-and-confirm scanner, Settings and themes

Owner requests (2026-11-18): "I'm not able to delete items in the inventory. I wonder if it is better to have separate
tabs for chores, meals, and inventory. The inventory section should have location information such as fridge,
pantry, basement, closet, etc. Maybe there's no need for a dedicated 'me' menu?" Then: delete should do **both**
(clear the stock, and remove the item entirely); "when it reads the barcode, it should stop scanning. Almost like a
loading menu while it tries to find the item"; "I want it modern. Kind of like Sam's Club scan to add to cart while
shopping"; "Is there a way to also give more themes? I want to take advantage of my AMOLED screen on my Pixel 10 Pro."

## Why

Food lives under Meals → Pantry and household items under Chores → Supplies, so "what do we have and where" is split
across two tabs with different controls, nothing can be deleted, and places can't be added (no Basement). The
scanner keeps scanning while it looks a code up and re-arms straight after, which feels jumpy. The owner reaches the
app mostly on a Pixel 10 Pro (AMOLED) inside the Home Assistant app, and on a desktop for recipe imports.

## Goals

- Bottom tabs (rail on ≥900 px): **Today · Meals · Inventory · Chores**. No Me tab.
- **Meals** pages: Plan, Recipes, Shopping (Pantry leaves; Shopping keeps showing running-low supplies as today).
- **Chores** pages: Chores only (Supplies leaves; the segmented page switcher disappears when a section has one page).
- **Inventory**: one page for food and household supplies, grouped by place, with place filter chips, Scan in the
  title row, per-item controls suited to the item, and two kinds of delete.
- **Places**: add, rename and remove Grocy locations from the app ("Manage places").
- **Settings** (⚙ button in the top bar on every page; at the bottom of the rail on wide screens): this device, theme,
  AI this month, People — what Me holds today.
- **Scanner** in the style of Sam's Club Scan & Go: full-screen camera, stop on read, a bottom sheet that confirms
  before anything changes, and a running "this trip" list with undo.
- **Themes**: Midnight (default), Black (AMOLED), Light, Match phone.

## Non-goals

- No change to how chores, reminders, meal planning, imports or Home Assistant work.
- No accent-colour picker, no per-person themes (themes stay per device).
- No changes to Home Assistant's own top bar (it follows HA's theme; a black HA theme is a separate job).

## Anti-goals (would make this a failure even if it works)

- Losing a stock or product by accident: Remove item always confirms, and never deletes something a recipe or
  chore still uses.
- A scan that changes stock without the reviewer seeing what it will do (the new flow always confirms).
- Black theme with grey panels everywhere (defeats the AMOLED point) or text that fails contrast.
- Breaking old links: `/pantry`, `/home/supplies`, `/me` keep working (redirect).

## Design

### Navigation

- `base.html` tabs become Today (`/`), Meals (`/plan`), Inventory (`/inventory`), Chores (`/home`). Meals' page
  switcher: Plan, Recipes, Shopping. Chores has one page, so no switcher.
- ⚙ Settings button (icon button, 48 px, label "Settings") in the top bar right; on the rail, a Settings link at the
  bottom. `/settings` holds the old Me rows: This device (Change), Theme, AI this month, People (link).
- Redirects (303 for GET): `/me` → `/settings`, `/pantry` → `/inventory`, `/home/supplies` → `/inventory`.
  Existing POST endpoints under `/pantry/…` and `/home/supplies/…` may stay or move; forms in the new page use
  whichever the builder keeps, and old POST paths that still exist must keep working.

### Inventory page (`/inventory`)

```
Inventory                                  [Scan]
[All] [Fridge] [Freezer] [Pantry] [Basement] …        ← chips: places that hold items, plus All
Use soon (food expiring, as on Pantry today)
Fridge
  Greek yogurt        500 g left · use by Fri
  Used [___] g [Use]                      More ▸
Closet
  Paper towels        3 left · keep at least 2
  [Used one]                              More ▸
Manage places
```

- Food rows keep Pantry's controls (Used [amount] Use, best-before line). Supply rows keep Supplies' controls (Used
  one, Running low state). Supplies with zero stock still show (they matter for Running low), food with zero stock
  doesn't (as today).
- **More** (a disclosure, or a bottom sheet with JS) holds: Set total; Move to [place]; Keep at least (supplies);
  **Clear stock** ("You have none left. It stays known, so scanning it again fills in the details, and it can still go
  on the shopping list."); **Remove item** (confirm dialog naming the item: "Remove Greek yogurt? Its stock, barcode
  and details are deleted. This can't be undone.").
- Remove item refuses with a plain message when a recipe ingredient or a chore uses the product (or its generic, when
  the row is a generic): "Fudgy brownies and Change the litter use this. Change them first, or use Clear stock."
  Removing a brand product leaves its generic alone; removing a generic is allowed only when no recipe/chore uses it
  and it has no brand products left.
- Chip filter: All shows every place group; a chip shows only that place (works without JS via `?place=<id>`).
  Remembered per device is not needed.
- **Manage places** (`/inventory/places`): list of places with Rename, Remove (only when empty: "Move or remove the
  3 items in Basement first."), and Add a place. Places are Grocy locations; `LOCATIONS` in setup_grocy stays the
  starting set. New places are usable everywhere a location is chosen (Move to, scan sheet, manual product form).
- Food vs supply is the existing product-group rule; the page just mixes them by place.

### Scanner (Scan from Inventory; `/scan`)

```
┌──────────────────────────┐        code read: short vibration (navigator.vibrate where
│ ✕                3 added │        supported), frame holds, scanning stops
│                          │                  ↓
│      ┌────────────┐      │        ┌──────────────────────────┐
│      │  [ frame ] │      │        │ ▔▔▔ bottom sheet ▔▔▔▔▔▔ │
│      └────────────┘      │        │ Looking up… (shimmer)     │
│                          │        │ → [photo] Greek yogurt    │
│ ( Add to stock | Use up )│        │   Counts as yogurt        │
└──────────────────────────┘        │ Fridge ▾      − [ 1 ] +   │
                                    │ [   Add to Fridge     ]   │
                                    │ Cancel                    │
                                    └──────────────────────────┘
```

- Full-screen camera with a visible scan frame; Add to stock / Use up toggle at the bottom; ✕ returns to Inventory.
- On a read: stop decoding (no second lookup while the sheet is up), vibrate once, show the sheet in a loading state
  ("Looking up…" with a shimmer; reduced motion: no shimmer, just the text).
- **Lookup is separate from changing stock.** A new lookup step resolves the code to a product (today's resolution,
  which may create the product from barcode databases/Claude, as now) and returns name, generic, photo if any, unit,
  package size, place, food/supply, current stock. Nothing is added or used yet.
- The sheet shows the product, place picker (Add only; defaults to the product's place), a −/+ stepper in packages
  (default 1; when the package size is unknown, an amount field in the stock unit instead, remembered as today), and
  a primary button "Add to Fridge" / "Use 1". Supplies: Use up is always one unit (today's rule). A new supply asks
  Keep at least in the same sheet (today's need_min step). "Counts as" can be fixed from the sheet (today's relink).
- Unknown barcode: the same sheet offers "Take a photo of the package" and the short manual form (today's flows),
  instead of separate sections of the page.
- After confirming: the sheet closes, a short toast ("Added 2 Greek yogurt to Fridge"), the trip counter goes up,
  scanning resumes. Cancel closes the sheet and resumes scanning without changing anything (a product created by the
  lookup stays known; that's fine).
- **This trip**: the counter ("3 added", "2 used") opens a sheet listing this session's changes, each with Undo
  (Grocy's stock booking undo). The trip lives in the page (sessionStorage is fine), not the server.
- Camera permission errors keep today's message and the Open scanner in browser link.

### Themes

- Settings → Theme: Midnight · Black · Light · Match phone. Stored per device in the existing `theme` cookie;
  `html[data-theme]` drives the tokens (as today). Match phone = Light when the phone is in light mode, Black when
  in dark mode (`prefers-color-scheme`).
- **Black**: `--bg: #000`; surfaces also #000 (cards separated by a 1px `--line` outline instead of a lighter fill);
  rail #000 with a divider; fields slightly lifted only where needed for affordance. Text and accents meet WCAG AA on
  black. `color-scheme: dark`, and `<meta name="theme-color">` follows the theme (black for Black) so the browser
  chrome matches.

### Style ("modern")

- Load `frontend-design:frontend-design` for every UI task. Keep the Midnight tokens and type; modern here means
  bottom sheets, steppers, chips, smooth sheet transitions (respecting reduced motion), large touch targets, no
  full-page reloads inside the scanner.
- House rules still apply: no inline styles, 48 px targets, sentence case, no middle dots or arrows in UI text (the
  sketches above use them only as diagram notation), no horizontal scroll from 320 px.

## Approaches considered

1. **Merge into one Inventory tab (chosen).** One place-grouped list; controls per item kind. Matches the owner's
   mental model ("where is it"), removes the split, gives places a home.
2. Keep Pantry and Supplies as two pages under a new Inventory tab. Less work, but keeps the split the owner noticed
   and can't group a basement shelf holding both food and supplies.
3. Add delete/places to the existing pages only. Fixes the bug but not the navigation the owner asked about.

Scanner: confirm-before-change (chosen, owner agreed) vs today's instant add. Confirm costs one tap per item and
buys quantity, place, and no accidental changes.

## Calls made after the builder check (designer's calls, not the owner's)

1. **Places are live Grocy data.** Startup seeds `LOCATIONS` only when Grocy has no locations yet (not by name on
   every start), so a renamed or removed starting place stays that way. Every place check (scan, import review,
   manual product form, Move) validates against Grocy's current locations. The fallback place for a new product is
   "Pantry" if it exists, else the first place. Any place may hold food or supplies; `HOME_LOCATIONS` only seeds.
2. **Rows follow today's rules.** Food: one row per product per place that holds stock (brands as today's Pantry).
   Supplies: one row per generic with its total (as today's Supplies), shown under the place holding most of its
   stock, or its default place when it has none. Set total and Clear stock act on the product across all places
   (today's Pantry behaviour); Move moves that row's amount from that place.
3. **Removing a place** needs no stock left in it. Products whose default place it is get a new default the person
   picks in the confirm ("Items that live here move to: [place]"), so zero-stock items can't block it invisibly.
4. **"Uses this product"** = a recipe ingredient or a chore that consumes it (its product, or its generic for a
   generic row). A brand is not blocked by recipes using its generic. Its barcodes, shopping-list entries and
   userfields are cleaned up with it; meal-plan entries point at recipes, not products, so they are unaffected.
5. **The place picked on the scan sheet** applies to that booking only; the product's default place doesn't change.
   Use up consumes in Grocy's default order; the sheet shows the total left.
6. **Undo** reverses only the stock booking (Grocy's undo). A remembered package size, a Keep at least value, a
   relink and a product the lookup created all stay. If Grocy refuses (the stock has since been used), the trip list
   says "Can't undo: already used" and keeps the line.
7. **Theme cookie values**: `dark` (Midnight, the default and what old cookies already hold), `black`, `light`,
   `auto` (Match phone). `auto` is pure CSS (`prefers-color-scheme`), no JS needed.
8. **Use soon** stays food only.

## Left to the builder

- Endpoint names for lookup, confirm, undo, places; whether the More panel is a disclosure or a sheet.
- How the trip's undo identifies a booking (Grocy returns stock log/transaction ids on add/consume).
- Exact Black-theme token values within the rules above.
- Test structure (TDD; existing tests are the contract except for the routes this design moves on purpose).
