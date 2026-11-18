from dataclasses import dataclass, field

import httpx

FDC_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
NUTRIENTS = {1008: "kcal_100", 1003: "protein_100", 1005: "carbs_100", 1004: "fat_100"}
KCAL_FALLBACKS = (2047, 2048)  # Atwater energy ids; Foundation foods often lack 1008


@dataclass
class Macros:
    kcal: float
    protein: float
    carbs: float
    fat: float
    missing: list[str] = field(default_factory=list)


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def _num(v) -> float:
    return 0.0 if _blank(v) else float(v)


def recipe_macros(positions: list[dict], product_info: dict[int, dict], base_servings: float) -> Macros:
    servings = float(base_servings or 1)
    tot = {"kcal_100": 0.0, "protein_100": 0.0, "carbs_100": 0.0, "fat_100": 0.0}
    missing: list[str] = []
    for pos in positions:
        info = product_info.get(int(pos["product_id"])) or {}
        if _blank(info.get("kcal_100")) or _blank(info.get("g_per_unit")):
            name = info.get("name") or f"product {pos['product_id']}"
            if name not in missing:
                missing.append(name)
            continue
        factor = float(pos["amount"]) * float(info["g_per_unit"]) / 100
        for k in tot:
            tot[k] += factor * _num(info.get(k))
    return Macros(tot["kcal_100"] / servings, tot["protein_100"] / servings,
                  tot["carbs_100"] / servings, tot["fat_100"] / servings, missing)


def fdc_lookup(name: str, api_key: str, http) -> dict | None:
    try:
        r = http.get(FDC_URL, headers={"User-Agent": "MealPlanner/1.0"},
                     params={"query": name, "dataType": "Foundation,SR Legacy", "pageSize": 1, "api_key": api_key})
        if r.status_code != 200:
            return None
        foods = r.json().get("foods") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not foods:
        return None
    by_id = {n.get("nutrientId"): n for n in foods[0].get("foodNutrients", []) if n.get("value") is not None}
    out = {key: by_id[nid]["value"] for nid, key in NUTRIENTS.items() if nid in by_id}
    if "kcal_100" not in out:
        for nid in KCAL_FALLBACKS:
            n = by_id.get(nid)
            if n and str(n.get("unitName", "")).upper() == "KCAL":
                out["kcal_100"] = n["value"]
                break
    return out or None


def fill_missing_nutrition(grocy, product_id: int, api_key: str, http) -> bool:
    if not _blank(grocy.userfields("products", product_id).get("kcal_100")):
        return False
    product = grocy.get_object("products", product_id)
    found = fdc_lookup(product["name"], api_key, http) if product else None
    if not found:
        return False
    grocy.set_userfields("products", product_id, found)
    return True
