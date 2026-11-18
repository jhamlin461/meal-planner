from datetime import date

import pytest


class FakeGrocy:
    """In-memory Grocy. entities: entity -> {id: row}; stock: product_id -> [entry dicts]."""

    def __init__(self):
        self.entities: dict[str, dict[int, dict]] = {}
        self.userfield_data: dict[tuple[str, int], dict] = {}
        self.stock: dict[int, list[dict]] = {}
        self.barcodes: dict[str, int] = {}  # barcode -> product id
        self.volatile_result: dict = {"due_products": [], "overdue_products": [],
                                      "expired_products": [], "missing_products": []}
        self._next_id = 1

    def _table(self, entity):
        return self.entities.setdefault(entity, {})

    def objects(self, entity, **where):
        return [dict(r) for r in self._table(entity).values()
                if all(str(r.get(k)) == str(v) for k, v in where.items())]

    def get_object(self, entity, id):
        row = self._table(entity).get(id)
        return dict(row) if row else None

    def create(self, entity, data):
        id = self._next_id
        self._next_id += 1
        self._table(entity)[id] = {**data, "id": id}
        return id

    def update(self, entity, id, data):
        self._table(entity)[id].update(data)

    def delete(self, entity, id):
        self._table(entity).pop(id, None)

    def userfields(self, entity, id):
        return dict(self.userfield_data.get((entity, id), {}))

    def set_userfields(self, entity, id, data):
        self.userfield_data.setdefault((entity, id), {}).update(data)

    def product_by_barcode(self, code):
        rows = self.objects("product_barcodes", barcode=code)
        pid = rows[0]["product_id"] if rows else self.barcodes.get(code)
        return {"product": self.get_object("products", pid)} if pid else None

    def stock_amounts(self):
        own = {pid: sum(e["amount"] for e in es) for pid, es in self.stock.items()}
        amounts = dict(own)
        for p in self.objects("products"):
            parent = p.get("parent_product_id")
            if parent and p["id"] in own:
                amounts[parent] = amounts.get(parent, 0.0) + own[p["id"]]
        return amounts

    def stock_entries(self):
        return [dict(e) for es in self.stock.values() for e in es]

    def add_stock(self, product_id, amount, best_before: date | None, location_id):
        self.stock.setdefault(product_id, []).append(
            {"product_id": product_id, "amount": amount,
             "best_before_date": best_before.isoformat() if best_before else None,
             "location_id": location_id})

    def consume(self, product_id, amount):
        left = amount
        entries = self.stock.get(product_id, [])
        while left > 0 and entries:
            take = min(left, entries[0]["amount"])
            entries[0]["amount"] -= take
            left -= take
            if entries[0]["amount"] <= 0:
                entries.pop(0)

    def set_amount(self, product_id, new_amount):
        self.stock[product_id] = ([{"product_id": product_id, "amount": new_amount,
                                    "best_before_date": None, "location_id": None}]
                                  if new_amount > 0 else [])

    def volatile(self, days):
        return self.volatile_result


@pytest.fixture
def fake_grocy_with_week():
    """Bootstrapped FakeGrocy: flour (10), recipe 1 (base 2, 150 g flour), 100 g stock,
    a Dinner entry 2026-11-19 for 4 servings, one Extras row."""
    from app.setup_grocy import bootstrap, ids

    g = FakeGrocy()
    bootstrap(g)
    i = ids(g)
    g._next_id = 100
    g.entities["products"] = {10: {"id": 10, "name": "Flour"}}
    g.entities["recipes"] = {1: {"id": 1, "name": "Bread", "base_servings": 2}}
    g.entities["recipes_pos"] = {1: {"id": 1, "recipe_id": 1, "product_id": 10, "amount": 150,
                                     "qu_id": i.unit["g"], "note": ""}}
    g.add_stock(10, 100, date(2027, 1, 1), i.location["Pantry"])
    g.create("meal_plan", {"day": "2026-11-19", "type": "recipe", "recipe_id": 1, "recipe_servings": 4,
                           "section_id": i.section["Dinner"], "note": "", "done": 0})
    g.create("shopping_list", {"shopping_list_id": i.list["Extras"], "product_id": 10, "amount": 1,
                               "done": 0, "note": "extra"})
    return g


class FakeAI:
    """Canned stand-in for app.ai.AI; set attributes per test, or set `error` to make every call raise."""

    def __init__(self, draft=None, guess=None, conversion=None, picks=None, week=None, error=None):
        self.draft, self.guess, self.conversion, self.picks, self.week = draft, guess, conversion, picks, week
        self.error = error

    def _ret(self, value):
        if self.error:
            raise self.error
        return value

    def structure_recipe(self, text=None, pdf=None, image=None):
        return self._ret(self.draft)

    def guess_product(self, info, generics, locations, image=None):
        return self._ret(self.guess)

    def guess_conversion(self, product_name, from_unit, to_unit):
        return self._ret(self.conversion)

    def match_ingredients(self, names, products):
        return self._ret(self.picks)

    def suggest_week(self, context):
        return self._ret(self.week)
