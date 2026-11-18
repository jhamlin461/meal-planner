from datetime import date, timedelta

from app.setup_grocy import ids
from app.walmart import packages_needed


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


def shortfall(needs: dict[int, float], stock: dict[int, float], staples: set[int]) -> dict[int, float]:
    out = {}
    for pid, need in needs.items():
        have = stock.get(pid, 0)
        if pid in staples and have > 0:
            continue
        if need - have > 1e-6:  # tolerate float noise (0.1*3 vs 0.3)
            out[pid] = need - have
    return out


def rebuild_meal_plan_list(grocy, today: date, shopping_weekday: int) -> dict[int, float]:
    start, end = shopping_window(today, shopping_weekday)
    entries = [e for e in grocy.objects("meal_plan")
               if e.get("day") and start <= date.fromisoformat(str(e["day"])[:10]) <= end]
    base = {int(r["id"]): (float(r.get("base_servings") or 0) or 1.0) for r in grocy.objects("recipes")}
    positions: dict[int, list[dict]] = {}
    for p in grocy.objects("recipes_pos"):
        positions.setdefault(int(p["recipe_id"]), []).append(p)
    needs = week_needs(entries, positions, base)
    staples = {pid for pid in needs if str(grocy.userfields("products", pid).get("staple")) == "1"}
    # compute everything before touching the list, so a failure can't leave it emptied
    short = shortfall(needs, grocy.stock_amounts(), staples)

    list_id = ids(grocy).list["Meal plan"]
    old = grocy.objects("shopping_list", shopping_list_id=list_id)
    kept: set[int] = set()
    for r in old:  # update in place so row ids (and done flags) stay stable for pages already open
        pid = int(r["product_id"]) if r.get("product_id") else None
        if pid in short and pid not in kept:
            kept.add(pid)
            if abs(float(r.get("amount") or 0) - short[pid]) > 1e-9:
                grocy.update("shopping_list", int(r["id"]), {"amount": short[pid]})
        else:  # no longer needed, free-text, or a duplicate row
            grocy.delete("shopping_list", int(r["id"]))
    for pid, amount in short.items():
        if pid not in kept:
            grocy.create("shopping_list", {"shopping_list_id": list_id, "product_id": pid, "amount": amount,
                                           "done": 0})
    return short


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
