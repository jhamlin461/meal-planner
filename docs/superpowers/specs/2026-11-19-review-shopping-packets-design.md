# Hidden weights, packets, accurate tables, a Shopping tab, and cooking from what we have

Owner requests (2026-11-19):
- "There should be an option to just add a bunch of items if they don't exist yet. Infer the weight of each item based
  on the product when I map it to a Walmart item. I never want to see the weight since it is only used for back-end
  information." Later: "always use Claude to assume initially, then adjust based on linked item."
- "When I go to review a recipe, it just takes me to the same import a recipe screen."
- "Should shopping be its own tab, since other items I am low on should also be automatically added to the shopping
  list as well."
- "Since I do EveryPlate still, I want the option to add a recipe's exact ingredients and amounts to my inventory. It is
  just an option, and not the default."
- "Is there a way for the meal plan to be suggested based on what I have?"
- "I need Claude to be 100% accurate with the cooking instructions." Then: "I don't need the word for word steps, since
  we are using the engineering format. Use this as an example: https://recipetables.com/r/3ad42061a8."
- "EveryPlate gives packets. In the recipe, if making an EveryPlate meal, include the measured amount needed and
  packets needed if that exists."
- "Don't forget to use that fancy front-end UI skill" (frontend-design) for the design.

## Why

The import review shows grams, conversion factors and one Create button per row, none of which the owner cares about:
he measures with cups and spoons and only wants the pantry to stay right in the background. He cooks from the
Cooking-for-Engineers table, so its correctness matters more than prose steps, and EveryPlate packets are how the food
actually arrives. Running low is a shopping concern for food as well as supplies.

## Goals

1. **Review link goes to the review.** Recipes' "Review N waiting" opens the first waiting import's review; after Save
   or Discard on a review, the next waiting import opens (or Recipes when none are left). The Import page keeps its
   list.
2. **Weights are never shown.** The import review shows, per ingredient, only the ingredient text and the product
   picker (and Create). No "Uses … g", no "1 cup =" line, no Suggest button, no conversion hint.
   - When a picked product's stock unit differs from the recipe unit and no conversion is known, Claude estimates it
     (the existing conversion guess) without asking; the estimate is used for the stock amount and stored as the
     product's conversion on Save. Estimates are cached on the pending import like the ingredient matches, so a reload
     costs nothing.
   - Packaging rows (packets) are no longer a reason to show a number: their amount comes from the card (see 7).
   - Free mode (no Claude): an ingredient whose conversion is unknown is saved without a stock amount (it isn't taken
     out of stock when cooked) and gets its estimate the first time Claude is available again; nothing is shown.
3. **Create all missing.** One button above the ingredient list creates a product for every row without one (the same
   creation each row's Create does), then re-renders with them picked. Each row's own Create stays.
4. **Linking a Walmart item adjusts the estimates.** When a Walmart link is saved for a product (Shopping, as today),
   Claude reads the product name and size from the link (the URL slug; the page title too if Walmart answers a plain
   request) and updates the product's package size and, for products counted in pieces, the grams per piece. The
   cups/spoons-to-grams estimate stays Claude's unless the link makes it obvious. Nothing about it is shown beyond the
   existing "Linked" state.
5. **Shopping is its own tab.** Tabs: Today, Meals, Inventory, Shopping, Chores (5 on phones; the rail on wide screens).
   Meals pages: Plan, Recipes. Food rows on Inventory get **Keep at least** (in More, as supplies have), so food below
   it lands on the Running low list automatically, exactly as supplies do now.
6. **Add ingredients to inventory** (recipe page, a secondary action, never automatic): adds the recipe's ingredients
   at the chosen servings to stock, each into its product's default place, after a confirm page listing what will be
   added in the recipe's own words ("2 tbsp umami ginger sauce (1 packet)"). Ingredients without a product, or without
   a known stock amount, are listed as skipped. Uses the same stock amounts the recipe already stores.
7. **Packets.** A table ingredient can carry `packets` (how many packets the card sends for the recipe's servings). The
   import prompt reads EveryPlate-style "what we send" columns (e.g. "1 | 2" for 2 | 4 servings) and fills `packets`
   for the column matching the recipe's servings, alongside the measured amount (qty + unit), which comes from the card
   or, when the card gives only packets, a sensible estimate marked "est." as today. Display everywhere the ingredient
   shows (table, review, add-to-inventory confirm): `2 tbsp umami ginger sauce (1 packet)`; packets scale with servings
   like amounts (0.5 packet shows as "half a packet", 1.5 as "1 1/2 packets"). Ingredients the household supplies
   itself (salt, oil) have no packets.
8. **The table reads like the example.** Prompt rules, from recipetables.com/r/3ad42061a8:
   - Ingredient notes say what the row is for when an ingredient is split ("for the chicken", "for searing", "raw,
     added later") and keep prep words after a comma ("garlic, minced").
   - Step labels are short imperatives with times and temperatures inline, semicolons between actions ("Sear 5 min,
     flip, cook 5 min more; remove", "Roast 425°F 18–20 min"), temperatures as the card writes them, no trailing
     period.
   - The final step spans the whole table (already true of the layout).
   - The recipe page stops showing the plain-text Steps section when a table exists (tableless recipes still show their
     steps).
9. **Accuracy check.** After structuring (in every mode that calls Claude), a second Claude pass re-reads the source and
   the table and returns a corrected table plus a list of doubts (ingredient row or step, and a short reason). It must
   check every amount, unit, packet count, temperature, time, and which ingredients each step combines, against the
   card. Corrections replace the table; doubts are shown on the review: the doubtful cells get a visible "Check this"
   mark and a one-line reason under the table, next to the PDF on wide screens. Doubts clear when the reviewer edits
   that row/step or saves. The check uses the same model/effort as structuring in full mode and the economy settings in
   economy; batch imports run their check as a normal call when the batch result arrives.
10. **Cook from what I have.** Plan gets a second suggest action, "Cook from what we have": fills open slots with
   recipes ranked by how much of each recipe is in stock (fully stocked first), then expiring stock, never repeating a
   recipe in the week, skipping recipes cooked in the last 3 weeks unless nothing else is fully stocked. Uses Claude in
   full/economy (the existing week suggestion with a "stock first" instruction and the same context), the local picker
   in free mode. Stock added with "Add ingredients to inventory" counts like any stock. The existing Suggest stays.

11. **Usable measurements everywhere a person sees or types an amount** (owner, 2026-11-19: "in the keep at least, I
   only want to see usable measurements. The conversion should happen behind the scenes. Example, keep 3 rolls of
   paper towels, or 3 cups of flour"). Each product gets a household measure it is counted in: a container or count
   word for supplies and packaged goods ("rolls", "bags", "cans", "bottles", "eggs") or a kitchen measure for loose food
   ("cups", "tbsp", "lb", "oz"), with the conversion to its stock unit (g, ml, piece) kept behind the scenes. Claude
   picks the measure and estimates the conversion when the product is created (scan, manual form, review, Create all
   missing) and refines it when a Walmart item is linked (goal 4); existing products get theirs the first time they are
   shown (cached, a cent or so each, at most once). Shown and typed in that measure:
   - Keep at least ("Keep at least [3] rolls", "[3] cups").
   - Inventory amounts and Use ("2 1/2 cups left", "Used [ ] cups"), Set total, the scan sheet's "left" line and its
     amount field when the package size is unknown.
   - Amounts are rounded to kitchen-friendly fractions (1/4, 1/3, 1/2…) as the recipe table already does.
   (Designer's call: the owner named Keep at least; the same rule is applied to every amount on Inventory and Scan so
   no gram value is ever shown or typed.) Free mode: a product without a measure yet shows its stock unit until Claude is
   back.

## Non-goals

- No change to how steps are crossed off, the scanner, chores, or Home Assistant.
- No grams display anywhere for cooking (no Cups/Grams toggle like the example).
- No scraping of Walmart beyond reading what the link and one plain page request give; no Walmart API.
- The Chrome "ERR_BLOCKED_BY_CLIENT" on the PDF is the owner's browser (an extension or policy), not the app; no code
  change. (The server sends a valid inline PDF; the "Open the PDF in a new tab" link stays.)

## Anti-goals

- A number the owner must understand or type to save a recipe (weights, factors) — the review must save with only
  products picked.
- The check pass silently changing the table without the reviewer being able to see what it doubted.
- Add ingredients to inventory happening without the confirm page, or by default on Save.

## Style

Load `frontend-design:frontend-design` for every UI task; keep the Midnight/Black/Light tokens and the app's type. The
recipe table is the memorable element: amounts set in tabular figures and bold, then the ingredient name, then the
purpose note muted; the packet count as a small outlined pill after the amount ("1 packet"); step cells on the
surface colour with the where-line under the label (as now). "Check this" marks: a dashed outline in the due-soon
colour on the cell plus a numbered marker that matches the reason list under the table (numbered because the reasons
are a list keyed to cells). The Shopping tab keeps the current Shopping page's groups. House rules: no inline styles,
48 px targets, sentence case, no middle dots or arrows in UI text, no overlap or sideways scroll from 320 px.

## Approaches considered

- Accuracy: (a) **verify pass with visible doubts (chosen)**; (b) only a stronger model/effort — no way to see what's
  uncertain; (c) word-for-word steps — the owner doesn't want them.
- Hidden weights: (a) **Claude estimates silently, Walmart link refines (chosen, owner's words)**; (b) keep asking the
  reviewer — what he asked to remove.
- Packets: (a) **a `packets` field on table ingredients (chosen)**; (b) packets in the note text — can't scale with
  servings or feed inventory.

## Calls made after the builder check (designer's calls, not the owner's)

1. **Cook from what we have** counts each ingredient as stocked when stock covers its amount at the servings the slot
   will use (the household's usual servings); ingredients with no stock amount (no conversion yet) are ignored, not
   counted missing.
2. **Walmart links** stay on the product the Shopping row shows (as today). Link-derived values fill blanks and replace
   Claude's estimates, but never a value the owner typed or a scan's barcode data gave (each stored value remembers
   where it came from: estimate, link, owner/scan).
3. **Refined conversions apply going forward**: saved recipes keep their stored amounts (no recompute job).
4. **The check pass edits in place**: it may change amounts, units, packets, notes, step labels/where and which inputs a
   step combines, but not add, remove or reorder ingredient rows or steps. Anything missing or extra becomes a doubt.
   It also runs after "Make the table".
5. **Free mode shows nothing about checking** (free-mode imports have no table to check).
6. **Add ingredients to inventory** books each ingredient on the recipe's product (the generic) with that product's
   default best-before and place; the result message offers Undo for that whole add (the scan's undo, per booking).
7. **Packets in stock**: a packet becomes its measured amount converted to the product's stock unit; a product counted
   in pieces of packets ("sauce packet") takes 1 piece per packet.
8. **No backfill**: old recipes have no packets and stay as they are.
9. **Food Keep at least** is set only in Inventory's More (no prompt when stocked). On Shopping, a product needed by
   both the meal plan and Keep at least is listed once, for enough to cook the plan and still keep the minimum.

## Left to the builder

- Endpoint names; how the check's doubts are keyed (row index / op id) and stored on the pending import.
- Exact prompt wording within the rules above; whether "Cook from what we have" is a separate button or an option of
  Suggest.
- How a free-mode ingredient without a conversion is stored so cooking skips it and a later estimate can fill it in.
