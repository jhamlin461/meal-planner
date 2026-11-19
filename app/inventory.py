"""Inventory: food and household supplies by place. Places are Grocy's own locations; nothing here caches them."""
from dataclasses import dataclass
from datetime import date

from app import measure
from app.grocy import GrocyError
from app.setup_grocy import LOCATIONS, ids, supplies_group
from app.today import _day

NEVER = "2999-12-31"  # Grocy's best-before for "never expires"


def places(grocy) -> dict[str, int]:
    """name -> id: the starting places first, in their usual order, then any others by name."""
    have = ids(grocy).location
    first = [n for n in LOCATIONS if n in have]
    return {n: have[n] for n in first + sorted((n for n in have if n not in first), key=str.lower)}


def default_place(grocy) -> str:
    p = places(grocy)
    return "Pantry" if "Pantry" in p else next(iter(p), "")


def _clean(grocy, name: str, own_id: int | None = None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Give the place a name.")
    if any(n.lower() == name.lower() and i != own_id for n, i in places(grocy).items()):
        raise ValueError(f"There's already a place called {name}.")
    return name


def add_place(grocy, name: str) -> int:
    return grocy.create("locations", {"name": _clean(grocy, name)})


def rename_place(grocy, id: int, name: str) -> None:
    grocy.update("locations", id, {"name": _clean(grocy, name, id)})


def remove_place(grocy, id: int, move_to: int) -> None:
    have = places(grocy)
    if len(have) <= 1:
        raise ValueError("Keep at least one place.")
    if move_to == id or move_to not in have.values():
        raise ValueError("Pick another place to move products to.")
    if any(int(e["location_id"]) == id and float(e["amount"]) > 0 for e in grocy.stock_entries()):
        label = next((n for n, i in places(grocy).items() if i == id), "that place")
        raise ValueError(f"Move or use up what's in {label} first.")
    for p in grocy.objects("products"):
        if p.get("location_id") is not None and int(p["location_id"]) == id:
            grocy.update("products", int(p["id"]), {"location_id": move_to})
    grocy.delete("locations", id)


def move_family(grocy, pid: int, from_location: int, to_location: int) -> None:
    """Move all of a generic's own and its brands' stock in one place to another."""
    family = {pid} | {int(p["id"]) for p in grocy.objects("products") if _int(p.get("parent_product_id")) == pid}
    held: dict[int, float] = {}
    for e in grocy.stock_entries():
        if int(e["product_id"]) in family and _int(e.get("location_id")) == from_location:
            held[int(e["product_id"])] = held.get(int(e["product_id"]), 0.0) + float(e["amount"])
    for k, amount in held.items():
        if amount > 0:
            grocy.transfer(k, amount, from_location, to_location)


@dataclass
class Inventory:
    soon: list[dict]  # food past or near its date, Today's words
    groups: dict[str, list[dict]]  # place name -> rows, in places() order, empty places left out
    totals: dict[int, float]  # product id -> its stock across places


def _when(bb, verb: str, today: date) -> tuple[str, bool]:
    """("Use by Friday" / "Best before Friday", past?)."""
    try:
        d = date.fromisoformat(str(bb)[:10])
    except ValueError:
        return "", False
    if d < today:
        return "Past its date", True
    return f"{verb} {'by ' if verb == 'Use' and d > today else ''}{_day(d, today)}", False


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def rows(grocy, settings, today: date) -> Inventory:
    """Food: one row per product per place with stock. Supplies: one row per generic per place holding it or its
    brands (that place's share; Running low counts the total), or one row at its default place when it has none.
    Each row's `m` is its product's household measure (its stock unit until it has one)."""
    products = {int(p["id"]): p for p in grocy.objects("products")}
    i = ids(grocy)
    units, locs, group = {v: k for k, v in i.unit.items()}, {v: k for k, v in i.location.items()}, supplies_group(grocy)
    # brands copy their generic's group, so a product's own group is enough
    is_food = lambda pid: group is None or str(products.get(pid, {}).get("product_group_id")) != str(group)  # noqa: E731
    generic = lambda pid: _int(products.get(pid, {}).get("parent_product_id")) or pid  # noqa: E731

    def row(pid: int, loc: int, kind: str) -> dict:
        p = products.get(pid, {})
        return {"pid": pid, "loc": loc, "name": p.get("name", f"product {pid}"), "amount": 0.0, "bb": NEVER,
                "unit": units.get(_int(p.get("qu_id_stock")), ""), "kind": kind, "when": "", "past": False,
                "gid": generic(pid)}

    vol = grocy.volatile(settings.expiry_warn_days)
    soon = [{"name": products.get(int(r["product_id"]), {}).get("name", f"product {r['product_id']}"),
             "when": w, "past": past}
            for key in ("expired_products", "overdue_products", "due_products") for r in vol.get(key) or []
            if is_food(int(r["product_id"])) for w, past in [_when(r.get("best_before_date"), "Use", today)]]
    found: dict[tuple[int, int], dict] = {}
    totals: dict[int, float] = {}
    family: dict[int, float] = {}  # a supply generic -> its own and its brands' stock, all places
    for e in grocy.stock_entries():
        pid, loc, amount = int(e["product_id"]), _int(e.get("location_id")) or 0, float(e["amount"])
        totals[pid] = totals.get(pid, 0.0) + amount
        if is_food(pid):
            r = found.setdefault((pid, loc), row(pid, loc, "food"))
            r["bb"] = min(r["bb"], str(e.get("best_before_date") or NEVER)[:10])
        else:
            r = found.setdefault((generic(pid), loc), row(generic(pid), loc, "supply"))
            family[r["pid"]] = family.get(r["pid"], 0.0) + amount
        r["amount"] += amount
    out = [r for r in found.values() if r["amount"] > 0]
    have = {r["pid"] for r in out}
    out += [row(pid, _int(p.get("location_id")) or 0, "supply") for pid, p in products.items()
            if not is_food(pid) and not p.get("parent_product_id") and pid not in have]
    for r in out:
        r["min"] = float(products.get(generic(r["pid"]), {}).get("min_stock_amount") or 0)  # kept on the generic
        if r["kind"] == "food":
            r["when"], r["past"] = _when(r["bb"], "Best before", today) if r["bb"] != NEVER else ("", False)
        else:
            r["total"] = family.get(r["pid"], 0.0)
            r["low"] = r["total"] < r["min"]
    measures: dict[int, measure.Measure] = {}
    for r in out:  # stored measures only: Claude is asked after the page is sent (see web's inventory)
        for key, pid in (("m", r["pid"]), ("km", r["gid"])):  # km: the generic's, where Keep at least is typed
            if pid not in measures:
                measures[pid] = measure.of(grocy, None, products.get(pid, {"id": pid}),
                                            units.get(_int(products.get(pid, {}).get("qu_id_stock")), ""))
            r[key] = measures[pid]
    order = {n: k for k, n in enumerate(places(grocy))}
    groups: dict[str, list[dict]] = {}
    for r in sorted(out, key=lambda r: (order.get(locs.get(r["loc"]), len(order)), r["bb"], r["name"].lower())):
        groups.setdefault(locs.get(r["loc"], "Other"), []).append(r)
    return Inventory(soon, groups, totals)


def _brands(grocy, pid: int) -> list[dict]:
    return [p for p in grocy.objects("products") if _int(p.get("parent_product_id")) == pid]


def _family(grocy, pid: int) -> list[int]:
    """What Clear stock and Remove act on. A supply generic hides its brands, so they go with it (brands first);
    a food generic's brands are rows of their own, so food is just the product."""
    group, p = supplies_group(grocy), grocy.get_object("products", pid)
    if group is None or str(p.get("product_group_id")) != str(group):
        return [pid]
    return [int(b["id"]) for b in _brands(grocy, pid)] + [pid]


def users_of(grocy, pid: int) -> list[str]:
    """Names of the recipes and chores that use this product (a chore only when it uses the product up)."""
    recipes = {int(r["id"]): r["name"] for r in grocy.objects("recipes")}
    made = {recipes[int(x["recipe_id"])] for x in grocy.objects("recipes_pos", product_id=pid)
            if _int(x.get("recipe_id")) in recipes}
    done = {c["name"] for c in grocy.objects("chores", product_id=pid) if str(c.get("consume_product_on_execution")) == "1"}
    return sorted(made, key=str.lower) + sorted(done - made, key=str.lower)  # recipes first, then chores


def _zero(grocy, pid: int) -> None:
    if any(int(e["product_id"]) == pid and float(e["amount"]) > 0 for e in grocy.stock_entries()):
        grocy.set_amount(pid, 0)


def clear_stock(grocy, pid: int) -> None:
    """Stock to 0 in every place, for a supply generic its brands' too; the product stays."""
    for k in _family(grocy, pid):
        _zero(grocy, k)


def why_not_remove(grocy, pid: int) -> str:
    """The reason a product can't be removed (a food generic still has brands, or a recipe or chore uses it or,
    for a supply, one of its brands), or ""."""
    family = _family(grocy, pid)
    brands = [b["name"] for b in _brands(grocy, pid)] if family == [pid] else []  # food generics only
    if brands:
        if len(brands) == 1:
            return f"{brands[0]} counts as this. Remove it first."
        return f"{', '.join(brands[:-1])} and {brands[-1]} count as this. Remove them first."
    used = list(dict.fromkeys(n for k in family for n in users_of(grocy, k)))
    if not used:
        return ""
    if len(used) == 1:
        return f"{used[0]} uses this. Change it first, or use Clear stock."
    return f"{', '.join(used[:-1])} and {used[-1]} use this. Change them first, or use Clear stock."


def remove_product(grocy, pid: int) -> None:
    """Delete a product with its stock, barcodes and shopping list rows; a generic goes with its brands.
    Refused while a recipe or chore uses it (or, for a generic, any of its brands). Each product is zeroed, then
    deleted, then its barcodes and list rows, so a Grocy refusal never leaves a product stripped of its rows."""
    if why := why_not_remove(grocy, pid):
        raise ValueError(why)
    done: list[str] = []
    for k in _family(grocy, pid):
        name = grocy.get_object("products", k)["name"]
        _zero(grocy, k)
        try:
            grocy.delete("products", k)
        except GrocyError as e:
            first = ""
            if done:
                first = f" Removed {', '.join(done[:-1])} and {done[-1]} first." if len(done) > 1 else f" Removed {done[0]} first."
            raise ValueError(f"Grocy wouldn't remove {name}: {e.reason or e}.{first}") from e
        done.append(name)
        for entity in ("product_barcodes", "shopping_list"):
            for r in grocy.objects(entity, product_id=k):
                grocy.delete(entity, int(r["id"]))
