from dataclasses import dataclass

import httpx

from app.ai import AIPaused, ProductGuess
from app.inventory import default_place, places
from app.measure import fields as measure_fields, fixed
from app.setup_grocy import ids, supplies_group

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{}.json"
UPC_URL = "https://api.upcitemdb.com/prod/trial/lookup"
NUTRIENTS = {"kcal_100": "energy-kcal_100g", "protein_100": "proteins_100g",
             "carbs_100": "carbohydrates_100g", "fat_100": "fat_100g"}


class NeedsDetails(Exception):
    """Free mode: Claude can't fill in a new product, so the scan screen asks for its details."""

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.name = name


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


def generic_choices(grocy) -> list[dict]:
    """Generics as Claude sees them: name plus stock unit, so a guess for an existing one uses its unit."""
    names = {v: k for k, v in ids(grocy).unit.items()}
    return [{"name": p["name"], "stock_unit": names.get(int(p.get("qu_id_stock") or 0), "")} for p in _generics(grocy)]


UNIT_FIELDS = ("qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price")
MEASURE = ("measure", "measure_factor", "measure_source", "measure_count", "measure_count_factor")


def _generic_named(grocy, name: str) -> dict | None:
    return next((p for p in _generics(grocy) if p["name"].lower() == name.lower()), None)


def _align_to_parent(grocy, parent: dict, fields: dict, uf: dict) -> dict:
    """Give a child its generic's units (Grocy sums child stock into the parent unconverted) and product group.
    Updates `fields`;
    returns the child's userfields; when those were in another unit, its generic's g_per_unit and measure instead."""
    unit = int(parent.get("qu_id_stock") or 0)
    same = int(fields.get("qu_id_stock") or 0) == unit
    fields.update(dict.fromkeys(UNIT_FIELDS, unit))
    fields["product_group_id"] = parent.get("product_group_id")  # Pantry or Supplies: wherever its generic is
    if same:
        return uf
    puf = grocy.userfields("products", int(parent["id"]))
    return {k: puf[k] for k in ("g_per_unit", *MEASURE) if puf.get(k) not in (None, "")}


def _nutrition(info: dict) -> dict:
    return {k: info[k] for k in NUTRIENTS if info.get(k) is not None}


def _find_or_create_generic(grocy, name: str, fields: dict, uf: dict, nutrition: dict) -> int:
    """fields: product columns to create the generic with; uf: userfields for a new generic."""
    parent = _generic_named(grocy, name)
    if parent:
        pid = int(parent["id"])
        if nutrition and grocy.userfields("products", pid).get("kcal_100") in (None, ""):
            grocy.set_userfields("products", pid, nutrition)
        return pid
    pid = grocy.create("products", {"name": name, **fields})
    grocy.set_userfields("products", pid, {**uf, **nutrition})
    return pid


def _product_fields(grocy, g, source: str = "estimate") -> tuple[dict, dict]:
    """(product columns, userfields) for a product described by a ProductGuess; `source` is where its package size
    came from (a scan's own data or a typed form: "owner")."""
    i = ids(grocy)
    unit = i.unit[g.stock_unit]
    loc = i.location[g.location if g.location in i.location else default_place(grocy)]
    fields = {"location_id": loc, "qu_id_stock": unit, "qu_id_purchase": unit, "qu_id_consume": unit,
              "qu_id_price": unit, "default_best_before_days": g.best_before_days}
    if g.category == "supply":  # household supplies never expire
        fields |= {"product_group_id": supplies_group(grocy), "default_best_before_days": -1}
    gpu = g.g_per_unit if g.g_per_unit is not None else (None if g.stock_unit == "piece" else 1)
    m = fixed(getattr(g, "measure", ""), getattr(g, "measure_factor", None), g.stock_unit)
    uf = {**({"package_amount": g.package_amount, "package_source": source}
             if g.package_amount and g.package_amount > 0 else {}),
          **({"g_per_unit": gpu} if gpu is not None else {}),  # unknown size: the scan screen asks for the amount
          **(measure_fields(m, "estimate") if m else {})}  # none (free mode): asked once Claude is back
    return fields, uf


def _create(grocy, g, name: str, code: str | None, nutrition: dict, source: str) -> Resolved:
    """The generic (found or new) and a product `name` under it with barcode `code`. Without a code, a name that is
    the generic's own makes just the generic. `source`: where the package size came from (see _product_fields)."""
    fields, uf = _product_fields(grocy, g, source)
    parent = _generic_named(grocy, g.generic_name)
    if parent:
        uf = _align_to_parent(grocy, parent, fields, uf)
    parent_id = _find_or_create_generic(grocy, g.generic_name, fields, uf, nutrition)
    if code is None and name.lower() == g.generic_name.lower():
        return Resolved(parent_id, g.generic_name, g.generic_name, True)
    if any(p["name"].lower() == name.lower() for p in grocy.objects("products")):
        name = f"{name} ({code or g.generic_name})"
    pid = grocy.create("products", {"name": name, **fields, "parent_product_id": parent_id})
    grocy.set_userfields("products", pid, {**uf, **nutrition})
    if code:
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
    try:
        g = ai.guess_product(info, generic_choices(grocy), list(places(grocy)))
    except AIPaused as e:
        raise NeedsDetails(info["name"]) from e
    # the barcode data's own size is the owner's; one Claude guessed without it is an estimate
    return _create(grocy, g, info["name"], code, _nutrition(info), "owner" if info.get("quantity_text") else "estimate")


def resolve_from_photo(grocy, ai, http: httpx.Client, code: str, image: bytes) -> Resolved:
    try:
        g = ai.guess_product({"barcode": code}, generic_choices(grocy), list(places(grocy)), image=image)
    except AIPaused as e:
        raise NeedsDetails() from e
    return _create(grocy, g, f"{g.generic_name} (scanned)", code, {}, "estimate")  # Claude read it off the photo


def create_manual(grocy, http: httpx.Client, code: str | None, name: str, generic: str, stock_unit: str,
                  package_amount: float | None, location: str, category: str) -> Resolved:
    """Free mode: the product a form describes (the scan screen's, or the import review's with no barcode),
    created as resolve() would from Claude's guess (shelf life from the generic it joins, else a week; nutrition
    from Open Food Facts when it has the code)."""
    if code and grocy.product_by_barcode(code):  # scanned twice before saving
        return resolve(grocy, None, http, code)
    parent = _generic_named(grocy, generic)
    days = int(float(parent.get("default_best_before_days") or 7)) if parent else 7
    g = ProductGuess.model_construct(generic_name=parent["name"] if parent else generic, is_new_generic=parent is None,
                                     stock_unit=stock_unit, package_amount=package_amount, location=location,
                                     best_before_days=days, g_per_unit=None, category=category)
    return _create(grocy, g, name, code, _nutrition(off_lookup(code, http) or {}) if code else {}, "owner")


def relink(grocy, product_id: int, generic_name: str) -> None:
    child = grocy.get_object("products", product_id)
    clash = next((p for p in grocy.objects("products") if p["name"].lower() == generic_name.lower()), None)
    if clash and clash.get("parent_product_id"):
        raise ValueError(f"'{generic_name}' is already a child product, not a generic; pick a generic name")
    keep = ("location_id", "qu_id_stock", "qu_id_purchase", "qu_id_consume", "qu_id_price",
            "default_best_before_days", "product_group_id")
    uf = grocy.userfields("products", product_id)
    parent = _generic_named(grocy, generic_name)
    upd: dict = {}
    if parent and int(parent.get("qu_id_stock") or 0) != int(child.get("qu_id_stock") or 0):
        if grocy.stock_amounts().get(product_id, 0.0) > 0:  # ponytail: no unit conversion of existing stock
            raise ValueError(f"{child['name']} is in stock in another unit than {parent['name']}; "
                             "use it up or set its amount to 0 first")
        fields = dict(child)
        new_uf = _align_to_parent(grocy, parent, fields, uf)
        upd = {k: fields[k] for k in UNIT_FIELDS}
        grocy.set_userfields("products", product_id, {**dict.fromkeys(("package_amount", "package_source", "g_per_unit",
                                                                        *MEASURE), ""), **new_uf})
    if parent:
        upd["product_group_id"] = parent.get("product_group_id")  # follows its generic between Pantry and Supplies
    new = {k: uf[k] for k in ("package_amount", "g_per_unit", *MEASURE) if uf.get(k) not in (None, "")}
    if "package_amount" in new:  # a new generic made from this brand keeps where its size came from
        new["package_source"] = uf.get("package_source") or "estimate"
    parent_id = _find_or_create_generic(grocy, generic_name, {k: child[k] for k in keep if k in child}, new, {})
    grocy.update("products", product_id, {**upd, "parent_product_id": parent_id})
