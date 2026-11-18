from dataclasses import dataclass

import httpx

from app.setup_grocy import LOCATIONS, ids

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{}.json"
UPC_URL = "https://api.upcitemdb.com/prod/trial/lookup"
NUTRIENTS = {"kcal_100": "energy-kcal_100g", "protein_100": "proteins_100g",
             "carbs_100": "carbohydrates_100g", "fat_100": "fat_100g"}


@dataclass
class Resolved:
    product_id: int
    name: str
    generic_name: str
    created: bool


def _get_json(http: httpx.Client, url: str, **kw) -> dict | None:
    try:
        r = http.get(url, headers={"User-Agent": "MealPlanner/1.0"}, **kw)
        data = r.json() if r.status_code == 200 else None
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def off_lookup(code: str, http: httpx.Client) -> dict | None:
    data = _get_json(http, OFF_URL.format(code))
    if not data or data.get("status") != 1:
        return None
    p, n = data.get("product") or {}, (data.get("product") or {}).get("nutriments") or {}
    return {"name": p.get("product_name"), "brand": p.get("brands"), "categories": p.get("categories_tags"),
            "quantity_text": p.get("quantity"), **{k: n.get(v) for k, v in NUTRIENTS.items()}}


def upc_lookup(code: str, http: httpx.Client) -> dict | None:
    items = (_get_json(http, UPC_URL, params={"upc": code}) or {}).get("items")
    if not items:
        return None
    return {"name": items[0].get("title"), "brand": items[0].get("brand"), "categories": None,
            "quantity_text": None, **dict.fromkeys(NUTRIENTS)}


def _generics(grocy) -> list[dict]:
    return [p for p in grocy.objects("products") if not p.get("parent_product_id")]


def _nutrition(info: dict) -> dict:
    return {k: info[k] for k in NUTRIENTS if info.get(k) is not None}


def _find_or_create_generic(grocy, name: str, fields: dict, uf: dict, nutrition: dict) -> int:
    """fields: product columns to create the generic with; uf: userfields for a new generic."""
    parent = next((p for p in _generics(grocy) if p["name"].lower() == name.lower()), None)
    if parent:
        pid = int(parent["id"])
        if nutrition and grocy.userfields("products", pid).get("kcal_100") in (None, ""):
            grocy.set_userfields("products", pid, nutrition)
        return pid
    pid = grocy.create("products", {"name": name, **fields})
    grocy.set_userfields("products", pid, {**uf, **nutrition})
    return pid


def _product_fields(grocy, g) -> tuple[dict, dict]:
    """(product columns, userfields) for a product described by a ProductGuess."""
    i = ids(grocy)
    unit = i.unit[g.stock_unit]
    loc = i.location[g.location if g.location in LOCATIONS else "Pantry"]
    fields = {"location_id": loc, "qu_id_stock": unit, "qu_id_purchase": unit, "qu_id_consume": unit,
              "qu_id_price": unit, "default_best_before_days": g.best_before_days}
    gpu = g.g_per_unit if g.g_per_unit is not None else (None if g.stock_unit == "piece" else 1)
    uf = {"package_amount": g.package_amount or 1, **({"g_per_unit": gpu} if gpu is not None else {})}
    return fields, uf


def _create(grocy, g, name: str, code: str, nutrition: dict) -> Resolved:
    fields, uf = _product_fields(grocy, g)
    parent_id = _find_or_create_generic(grocy, g.generic_name, fields, uf, nutrition)
    if any(p["name"].lower() == name.lower() for p in grocy.objects("products")):
        name = f"{name} ({code})"
    pid = grocy.create("products", {"name": name, **fields, "parent_product_id": parent_id})
    grocy.set_userfields("products", pid, {**uf, **nutrition})
    grocy.create("product_barcodes", {"product_id": pid, "barcode": code})
    return Resolved(pid, name, g.generic_name, True)


def resolve(grocy, ai, http: httpx.Client, code: str) -> Resolved | None:
    hit = grocy.product_by_barcode(code)
    if hit:
        p = hit["product"]
        parent = grocy.get_object("products", p["parent_product_id"]) if p.get("parent_product_id") else None
        return Resolved(int(p["id"]), p["name"], (parent or p)["name"], False)
    info = off_lookup(code, http)
    if not info or not info.get("name"):
        upc = upc_lookup(code, http)
        info = {**(info or {}), **{k: upc[k] for k in ("name", "brand")}} if upc else None
    if not info or not info.get("name"):
        return None
    g = ai.guess_product(info, [p["name"] for p in _generics(grocy)], LOCATIONS)
    return _create(grocy, g, info["name"], code, _nutrition(info))


def resolve_from_photo(grocy, ai, http: httpx.Client, code: str, image: bytes) -> Resolved:
    g = ai.guess_product({"barcode": code}, [p["name"] for p in _generics(grocy)], LOCATIONS, image=image)
    return _create(grocy, g, f"{g.generic_name} (scanned)", code, {})


def relink(grocy, product_id: int, generic_name: str) -> None:
    child = grocy.get_object("products", product_id)
    clash = next((p for p in grocy.objects("products") if p["name"].lower() == generic_name.lower()), None)
    if clash and clash.get("parent_product_id"):
        raise ValueError(f"'{generic_name}' is already a child product, not a generic; pick a generic name")
    keep = ("location_id", "qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price",
            "default_best_before_days")
    uf = grocy.userfields("products", product_id)
    parent_id = _find_or_create_generic(
        grocy, generic_name, {k: child[k] for k in keep if k in child},
        {k: uf[k] for k in ("package_amount", "g_per_unit") if k in uf}, {})
    grocy.update("products", product_id, {"parent_product_id": parent_id})
