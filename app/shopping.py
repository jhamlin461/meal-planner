from datetime import date, timedelta

from app.setup_grocy import ids
from app.walmart import packages_needed


def base_servings(recipe: dict) -> float:
    """A recipe's base servings; 2 when Grocy has none (the recipe page, shopping, cooking and meal kits all read it
    so)."""
    return float(recipe.get("base_servings") or 0) or 2.0


def shopping_window(today: date, shopping_weekday: int) -> tuple[date, date]:
    next_shop = today + timedelta(days=(shopping_weekday - today.weekday()) % 7)
    return today, next_shop + timedelta(days=7)


def _done(row: dict) -> bool:
    return bool(int(float(row.get("done") or 0)))


def week_needs(entries: list[dict], positions: dict[int, list[dict]],
               base_servings: dict[int, float]) -> dict[int, float]:
    needs: dict[int, float] = {}
    for e in entries:
        if e.get("type") != "recipe" or _done(e):
            continue
        rid = int(e["recipe_id"])
        if rid not in positions or rid not in base_servings:
            continue
        factor = float(e["recipe_servings"]) / float(base_servings[rid])
        for pos in positions[rid]:
            pid = int(pos["product_id"])
            needs[pid] = needs.get(pid, 0.0) + float(pos["amount"]) * factor
    return needs


def combined_need(meal_plan: dict[int, float], stock, mins: dict[int, float]) -> dict[int, float]:
    """Per product, enough to cook the plan and still keep the minimum: max(0, need + min - stock). `meal_plan` is what
    the plan needs (not what is short). `stock`: the amount to count per product, the only stock read (Task 7's meal
    kits pass free stock here); a product with a minimum is covered even with no plan."""
    out = {}
    for pid in meal_plan.keys() | mins.keys():
        short = meal_plan.get(pid, 0.0) + mins.get(pid, 0.0) - stock.get(pid, 0.0)
        if short > 1e-6:  # tolerate float noise (0.1*3 vs 0.3)
            out[pid] = short
    return out


def rebuild_lists(grocy, today: date, shopping_weekday: int, house=None) -> dict[int, float]:
    """Meal plan and Running low lists from one number per product (combined_need), so a product both planned and below
    its keep-at-least is on one list only (Meal plan), for both. Stock is what no meal kit holds (`house`'s kits),
    except a planned recipe's own kit."""
    start, end = shopping_window(today, shopping_weekday)
    entries = [e for e in grocy.objects("meal_plan")
               if e.get("day") and start <= date.fromisoformat(str(e["day"])[:10]) <= end]
    from app.inventory import free_stock, stock_lines  # inventory -> today -> mealplan -> shopping
    base = {int(r["id"]): base_servings(r) for r in grocy.objects("recipes")}
    planned = {int(e["recipe_id"]) for e in entries if e.get("type") == "recipe" and e.get("recipe_id")}
    positions = {rid: [{"product_id": line["pid"], "amount": line["amount"]} for line in stock_lines(grocy, rid, 1.0)[0]]
                 for rid in planned if rid in base}  # what cooking takes (mealplan.cook), so that is what is bought
    needs = week_needs(entries, positions, base)
    stock = free_stock(grocy, house, {int(e["recipe_id"]) for e in entries if e.get("type") == "recipe"
                                      and not _done(e)})
    staples = {pid for pid in needs if stock.get(pid, 0) > 0 and str(grocy.userfields("products", pid).get("staple")) == "1"}
    needs = {pid: n for pid, n in needs.items() if pid not in staples}
    mins = {int(p["id"]): float(p["min_stock_amount"]) for p in grocy.objects("products")
            if float(p.get("min_stock_amount") or 0) > 0}
    need = combined_need(needs, stock, mins)
    ticked = {int(r["product_id"]): float(r.get("amount") or 0) for n in ("Meal plan", "Running low")
              for r in grocy.objects("shopping_list", shopping_list_id=ids(grocy).list[n])
              if r.get("product_id") and _done(r)}  # a tick follows its product to the other list
    _sync_list(grocy, "Meal plan", {pid: a for pid, a in need.items() if pid in needs}, ticked)
    _sync_list(grocy, "Running low", {pid: a for pid, a in need.items() if pid not in needs}, ticked)
    return need


def _sync_list(grocy, name: str, wanted: dict[int, float], ticked: dict[int, float]) -> None:
    """Make list `name` one row per product in `wanted`, in place so row ids (and done flags) stay stable for pages
    already open. Callers compute `wanted` before this, so a failure can't leave the list emptied."""
    list_id = ids(grocy).list[name]
    kept: set[int] = set()
    for r in grocy.objects("shopping_list", shopping_list_id=list_id):
        pid = int(r["product_id"]) if r.get("product_id") else None
        if pid in wanted and pid not in kept:
            kept.add(pid)
            old_amt = float(r.get("amount") or 0)
            if abs(old_amt - wanted[pid]) > 1e-9:
                upd = {"amount": wanted[pid]}
                if wanted[pid] > old_amt:  # the tick covered the old amount only
                    upd["done"] = 0
                grocy.update("shopping_list", int(r["id"]), upd)
        else:  # no longer needed, free-text, or a duplicate row
            grocy.delete("shopping_list", int(r["id"]))
    for pid, amount in wanted.items():
        if pid not in kept:
            grocy.create("shopping_list", {"shopping_list_id": list_id, "product_id": pid, "amount": amount,
                                           "done": int(amount <= ticked.get(pid, 0.0) + 1e-9)})


def walmart_items(grocy, shortfall: dict[int, float]) -> tuple[list[tuple[str, int]], list[dict]]:
    cart, rest = [], []
    for pid, amount in shortfall.items():
        product = grocy.get_object("products", pid)
        if product is None:
            continue  # product deleted since the list was built
        uf = grocy.userfields("products", pid)
        if uf.get("walmart_item_id"):
            pkg = float(uf["package_amount"]) if uf.get("package_amount") else None
            cart.append((str(uf["walmart_item_id"]), packages_needed(amount, pkg)))
        else:
            rest.append(product)
    return cart, rest
