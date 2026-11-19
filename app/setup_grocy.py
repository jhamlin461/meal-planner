from dataclasses import dataclass

from app.config import load_settings
from app.grocy import Grocy

UNITS = ["g", "kg", "ml", "l", "piece", "cup", "tbsp", "tsp", "oz", "lb", "fl oz", "can", "pinch"]
GLOBAL_CONVERSIONS = {("kg", "g"): 1000, ("l", "ml"): 1000, ("cup", "ml"): 236.6, ("tbsp", "ml"): 14.79,
                      ("tsp", "ml"): 4.93, ("fl oz", "ml"): 29.57, ("oz", "g"): 28.35, ("lb", "g"): 453.6}
HOME_LOCATIONS = ["Bathroom", "Laundry", "Under sink", "Garage", "Closet"]  # where household supplies live
LOCATIONS = ["Fridge", "Freezer", "Pantry", "Spice rack", "Counter", *HOME_LOCATIONS]
SECTIONS = ["Lunch", "Dinner"]
LISTS = ["Meal plan", "Extras", "Running low"]
SUPPLIES = "Household supplies"  # product group
TEXT, MULTI, NUM, CHECK = "text-single-line", "text-multi-line", "number-decimal", "checkbox"
USERFIELDS = (
    [("recipes", "cfe_table", MULTI), ("recipes", "source_url", TEXT), ("recipes", "source_pdf", TEXT),
     ("recipes", "doubts", MULTI),
     ("products", "walmart_item_id", TEXT), ("products", "staple", CHECK)]
    + [("products", n, NUM) for n in ("package_amount", "g_per_unit", "kcal_100", "protein_100", "carbs_100", "fat_100",
                                      "measure_factor", "measure_count_factor")]
    # measure: the household measure ("cups"); *_source: where measure / package_amount came from (estimate, link,
    # owner); measure_count: a count word ("bottles") the product was in before a kitchen measure, kept to go back
    + [("products", n, TEXT) for n in ("measure", "measure_source", "package_source", "measure_count")]
)


@dataclass
class Ids:
    unit: dict[str, int]
    location: dict[str, int]
    section: dict[str, int]
    list: dict[str, int]


def _by_name(grocy, entity) -> dict[str, int]:
    return {r["name"]: int(r["id"]) for r in grocy.objects(entity)}


def ids(grocy) -> Ids:
    return Ids(_by_name(grocy, "quantity_units"), _by_name(grocy, "locations"),
               _by_name(grocy, "meal_plan_sections"), _by_name(grocy, "shopping_lists"))


def supplies_group(grocy) -> int | None:
    return next((g["id"] for g in grocy.objects("product_groups") if g["name"] == SUPPLIES), None)


def _ensure_named(grocy, entity, names, extra=lambda i, n: {}):
    have = _by_name(grocy, entity)
    for i, n in enumerate(names):
        if n not in have:
            grocy.create(entity, {"name": n, **extra(i, n)})


def bootstrap(grocy) -> None:
    _ensure_named(grocy, "quantity_units", UNITS, lambda i, n: {"name_plural": n})
    if not grocy.objects("locations"):  # only a fresh Grocy gets the starting places; after that they are the household's
        _ensure_named(grocy, "locations", LOCATIONS)
    _ensure_named(grocy, "meal_plan_sections", SECTIONS, lambda i, n: {"sort_number": i + 1})
    _ensure_named(grocy, "shopping_lists", LISTS)
    _ensure_named(grocy, "task_categories", ["Household"])
    _ensure_named(grocy, "product_groups", [SUPPLIES])
    unit = _by_name(grocy, "quantity_units")
    # filter in Python: Grocy's query[] can't express "product_id is null"
    have = {(int(c["from_qu_id"]), int(c["to_qu_id"])) for c in grocy.objects("quantity_unit_conversions")
            if c.get("product_id") is None}
    for (a, b), factor in GLOBAL_CONVERSIONS.items():
        if (unit[a], unit[b]) not in have:
            grocy.create("quantity_unit_conversions", {"from_qu_id": unit[a], "to_qu_id": unit[b],
                                                       "factor": factor, "product_id": None})
    have_uf = {(u["entity"], u["name"]) for u in grocy.objects("userfields")}
    for entity, name, typ in USERFIELDS:
        if (entity, name) not in have_uf:
            grocy.create("userfields", {"entity": entity, "name": name, "caption": name.replace("_", " ").title(),
                                        "type": typ, "show_as_column_in_tables": 0})


if __name__ == "__main__":
    s = load_settings()
    bootstrap(Grocy(s.grocy_url, s.grocy_api_key))
