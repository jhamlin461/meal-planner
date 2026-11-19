import calendar
from datetime import date, timedelta

import pytest


# Real Grocy's chores columns (its migrations, v4.7): writing any other name is a 400, as on the live server.
CHORE_COLUMNS = {"id", "name", "description", "period_type", "period_days", "period_config", "period_interval",
                 "track_date_only", "rollover", "assignment_type", "assignment_config",
                 "next_execution_assigned_to_user_id", "consume_product_on_execution", "product_id", "product_amount",
                 "start_date", "rescheduled_date", "rescheduled_next_execution_assigned_to_user_id", "active",
                 "row_created_timestamp"}


# Real Grocy's recipes_pos columns (migrations 0045, 0068 variable_amount, 0087 price_factor, 0249 round_up).
RECIPES_POS_COLUMNS = {"id", "recipe_id", "product_id", "amount", "note", "qu_id", "only_check_single_unit_in_stock",
                       "ingredient_group", "not_check_stock_fulfillment", "variable_amount", "price_factor",
                       "round_up", "row_created_timestamp"}
COLUMNS = {"chores": CHORE_COLUMNS, "recipes_pos": RECIPES_POS_COLUMNS}


def _columns(entity, data):
    from app.grocy import GrocyError
    bad = set(data) - COLUMNS[entity] if entity in COLUMNS else set()
    if bad:
        raise GrocyError(f"{entity} has no column {sorted(bad)}", 400,
                         f"table {entity} has no column named {min(bad)}")


class FakeGrocy:
    """In-memory Grocy. entities: entity -> {id: row}; stock: product_id -> [entry dicts]."""

    def __init__(self):
        self.entities: dict[str, dict[int, dict]] = {}
        self.userfield_data: dict[tuple[str, int], dict] = {}
        self.stock: dict[int, list[dict]] = {}
        self.barcodes: dict[str, int] = {}  # barcode -> product id
        self.volatile_result: dict = {"due_products": [], "overdue_products": [],
                                      "expired_products": [], "missing_products": []}
        self.user_rows: list[dict] = []
        self.today = date.today()  # tests may move this; schedule math is dates only
        self.chore_log: list[dict] = []
        self.battery_log: list[dict] = []
        self.pictures: dict[str, bytes] = {}  # recipe pictures by file name
        self.bookings: dict[str, tuple] = {}  # transaction id -> ("add"|"consume", [(stock entry, amount)])
        self._next_id = 1
        self._stock_ids = 0  # each stock entry's stock_id, as Grocy gives one

    def put_recipe_picture(self, filename, data):
        self.pictures[filename] = data

    def recipe_picture(self, filename, width):
        return (self.pictures[filename], "image/jpeg") if filename in self.pictures else None

    def delete_recipe_picture(self, filename):
        self.pictures.pop(filename, None)

    def users(self):
        return [dict(u) for u in self.user_rows]

    def create_user(self, username, first_name):
        uid = max([u["id"] for u in self.user_rows], default=0) + 1
        self.user_rows.append({"id": uid, "username": username, "first_name": first_name})
        return uid

    def _table(self, entity):
        return self.entities.setdefault(entity, {})

    def objects(self, entity, **where):
        return [dict(r) for r in self._table(entity).values()
                if all(str(r.get(k)) == str(v) for k, v in where.items())]

    def get_object(self, entity, id):
        row = self._table(entity).get(id)
        return dict(row) if row else None

    def create(self, entity, data):
        _columns(entity, data)
        id = self._next_id
        self._next_id += 1
        self._table(entity)[id] = {**data, "id": id}
        return id

    def update(self, entity, id, data):
        _columns(entity, data)
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

    def _book(self, kind, rows):
        tid = f"t{len(self.bookings) + 1}"
        self.bookings[tid] = (kind, rows)
        return tid

    def _stock_id(self):
        self._stock_ids += 1
        return f"s{self._stock_ids}"

    def add_stock(self, product_id, amount, best_before: date | None, location_id):
        return self.add_stock_entries(product_id, amount, best_before, location_id)[0]

    def add_stock_entries(self, product_id, amount, best_before: date | None, location_id):
        e = {"product_id": product_id, "amount": amount, "stock_id": self._stock_id(),
             "best_before_date": best_before.isoformat() if best_before else None, "location_id": location_id}
        self.stock.setdefault(product_id, []).append(e)
        return self._book("add", [(e, amount)]), [(e["stock_id"], amount)]

    def consume(self, product_id, amount, stock_entry_id=None):
        """Own stock first, then its sub products (Grocy's allow_subproduct_substitution, which app.grocy sends), each
        in Grocy's default order (soonest best-before first); with `stock_entry_id`, only that entry's rows (a split
        keeps the stock_id), and never more than they hold."""
        from app.grocy import GrocyError
        left, taken = amount, []
        kids = [p["id"] for p in self.objects("products") if p.get("parent_product_id") == product_id]
        if stock_entry_id and amount > sum(e["amount"] for e in self.stock.get(product_id, [])
                                           if e.get("stock_id") == stock_entry_id) + 1e-9:
            raise GrocyError("consume more than the entry holds", 400, "Amount to be consumed cannot be > than the "
                                                                       "amount of this stock entry")
        for pid in [product_id] if stock_entry_id else [product_id, *kids]:
            entries = self.stock.get(pid, [])
            for e in sorted((e for e in entries if not stock_entry_id or e.get("stock_id") == stock_entry_id),
                            key=lambda e: e.get("best_before_date") or "2999-12-31"):
                if left <= 1e-12:
                    break
                take = min(left, e["amount"])
                e["amount"] -= take
                left -= take
                taken.append((e, take))
            entries[:] = [e for e in entries if e["amount"] > 1e-12]
        return self._book("consume", taken)

    def undo(self, transaction_id):
        """Grocy's booking undo: an add's stock comes off (refused once any of it was used), a use's goes back."""
        from app.grocy import GrocyError
        kind, rows = self.bookings.get(transaction_id, (None, []))
        if kind == "add":
            [(e, amount)] = rows
            entries = self.stock.get(e["product_id"], [])
            if not (any(x is e for x in entries) and e["amount"] >= amount):
                kind = None  # some of it was used (or moved) since
        if kind not in ("add", "consume"):  # unknown, already undone, or used since
            raise GrocyError(f"undo {transaction_id} refused", 400, "This booking cannot be undone")
        self.bookings[transaction_id] = ("undone", [])  # kept, so ids are never reused
        if kind == "add":
            entries[:] = [x for x in entries if x is not e]
        for e, take in rows if kind == "consume" else []:
            entries = self.stock.setdefault(e["product_id"], [])
            if any(x is e for x in entries):
                e["amount"] += take
            else:
                entries.append({**e, "amount": take})

    def transfer(self, product_id, amount, from_location, to_location):
        for e in self.stock.get(product_id, []):
            if e["location_id"] == from_location and amount > 0:
                take = min(amount, e["amount"])
                if take < e["amount"]:
                    e["amount"] -= take
                    self.stock[product_id].append({**e, "amount": take, "location_id": to_location})
                else:
                    e["location_id"] = to_location
                amount -= take

    def set_amount(self, product_id, new_amount):
        """Grocy's inventory booking: the product's own stock becomes new_amount, the difference used in Grocy's
        default order or added at the product's place."""
        own = sum(e["amount"] for e in self.stock.get(product_id, []))
        if new_amount < own:
            kids = [p for p in self._table("products").values() if p.get("parent_product_id") == product_id]
            for k in kids:  # its own stock only, as Grocy's correction does
                k["parent_product_id"], k["_was"] = None, product_id
            try:
                self.consume(product_id, own - new_amount)
            finally:
                for k in kids:
                    k["parent_product_id"] = k.pop("_was")
        elif new_amount > own:
            self.add_stock(product_id, new_amount - own, None,
                           (self.get_object("products", product_id) or {}).get("location_id"))

    def volatile(self, days):
        """The fixed result tests set; once any product has a min stock amount, missing_products is computed
        (a parent with cumulate_min_stock_amount_of_sub_products counts its sub products' stock too)."""
        mins = [p for p in self.objects("products") if float(p.get("min_stock_amount") or 0) > 0]
        if not mins:
            return self.volatile_result
        total = self.stock_amounts()
        missing = []
        for p in mins:
            cumulate = int(p.get("cumulate_min_stock_amount_of_sub_products") or 0)
            have = total.get(p["id"], 0) if cumulate else sum(e["amount"] for e in self.stock.get(p["id"], []))
            if have < float(p["min_stock_amount"]):
                missing.append({"id": p["id"], "name": p["name"], "amount_missing": float(p["min_stock_amount"]) - have,
                                "is_partly_in_stock": int(have > 0)})
        return {**self.volatile_result, "missing_products": missing}

    _DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    def _next_due(self, c, last: date | None) -> date | None:
        pt = c.get("period_type", "manually")
        n = int(c.get("period_interval") or 1)
        if pt == "manually":
            return None
        if last is None:
            return self.today
        if pt in ("daily", "dynamic-regular"):
            return last + timedelta(days=n)
        if pt == "weekly":
            names = [d.strip() for d in str(c.get("period_config") or "").split(",") if d.strip()]
            for k in range(1, 8):
                d = last + timedelta(days=k)
                if self._DAYS[d.weekday()] in names:
                    return d
            return last + timedelta(days=7)
        if pt == "monthly":
            want = int(c.get("period_days") or 1)
            if last.day < want:
                y, m = last.year, last.month
            else:
                y, m = last.year + (last.month == 12), last.month % 12 + 1
            return date(y, m, min(want, calendar.monthrange(y, m)[1]))
        if pt == "yearly":
            return last + timedelta(days=365)
        raise ValueError(f"unknown period_type {pt!r}")

    def chores_status(self):
        out = []
        for c in self.objects("chores"):
            runs = [r for r in self.chore_log if r["chore_id"] == c["id"]]
            last = runs[-1]["date"] if runs else None
            due = self._next_due(c, last)
            out.append({"chore_id": c["id"], "chore_name": c.get("name"),
                        "next_estimated_execution_time": f"{due.isoformat()} 00:00:00" if due else None,
                        "next_execution_assigned_to_user_id": c.get("next_execution_assigned_to_user_id"),
                        "last_tracked_time": f"{last.isoformat()} 00:00:00" if last else None})
        return out

    def _assign(self, c) -> int | None:
        """Grocy's next assignee: who-least-did-first picks from assignment_config; no-assignment has none."""
        if c.get("assignment_type") != "who-least-did-first":
            return None
        ids = [int(x) for x in str(c.get("assignment_config") or "").split(",") if x.strip()]
        done = [r["done_by"] for r in self.chore_log if r["chore_id"] == c["id"]]
        return min(ids, key=lambda u: done.count(u)) if ids else None

    def calculate_next_assignments(self):
        for c in self._table("chores").values():
            c["next_execution_assigned_to_user_id"] = self._assign(c)

    def execute_chore(self, chore_id, done_by, skipped=False):
        c = self.get_object("chores", chore_id)
        self.chore_log.append({"chore_id": chore_id, "done_by": done_by, "skipped": skipped, "date": self.today})
        self._table("chores")[chore_id]["next_execution_assigned_to_user_id"] = self._assign(c)  # Grocy recalculates
        if not skipped and int(c.get("consume_product_on_execution") or 0) == 1:
            pid, amt = int(c["product_id"]), float(c.get("product_amount") or 1)
            if self.stock_amounts().get(pid, 0) >= amt:  # real Grocy rejects a short consume entirely
                self.consume(pid, amt)

    def open_tasks(self):
        return [t for t in self.objects("tasks") if int(t.get("done") or 0) == 0]

    def complete_task(self, task_id):
        self._table("tasks")[task_id]["done"] = 1

    def batteries_status(self):
        out = []
        for b in self.objects("batteries"):
            runs = [r for r in self.battery_log if r["battery_id"] == b["id"]]
            last = runs[-1]["date"] if runs else None
            days = int(b.get("charge_interval_days") or 0)
            due = (last or self.today) + timedelta(days=days) if days else None
            out.append({"battery_id": b["id"],
                        "next_estimated_charge_time": f"{due.isoformat()} 00:00:00" if due else None,
                        "last_tracked_time": f"{last.isoformat()} 00:00:00" if last else None})
        return out

    def charge_battery(self, battery_id):
        self.battery_log.append({"battery_id": battery_id, "date": self.today})


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

    def __init__(self, draft=None, guess=None, conversion=None, picks=None, week=None, error=None, mode="full",
                 check=None, measure=None, walmart=None):
        self.walmart = walmart  # read_walmart's answer (a WalmartRead); None: it fails as Claude would
        self.walmart_reads: list[tuple] = []  # every read_walmart call's (name, stock unit, text)
        self.measure = measure  # guess_measure's answer (a MeasureGuess); None: it fails as Claude would
        self.measures: list[tuple[str, str]] = []  # every guess_measure call's (name, stock unit)
        self.draft, self.guess, self.conversion, self.picks, self.week = draft, guess, conversion, picks, week
        self.check = check  # check_table's answer (a TableCheck, or an exception it raises); None: the table back
        self.checks: list[dict] = []  # every check_table call's arguments; `checked` is the last
        self.error = error
        self.mode_ = mode  # the budget mode app.ai.AI.mode() would report

    def mode(self, now=None):
        return self.mode_

    def _ret(self, value):
        if self.mode_ == "free":
            from app.ai import AIPaused
            raise AIPaused()
        if self.error:
            raise self.error
        return value

    def structure_recipe(self, text=None, pdf=None, image=None):
        self.seen_text, self.seen_pdf = text, pdf
        return self._ret(self.draft)

    def check_table(self, table, servings, text=None, pdf=None, image=None):
        from app.ai import TableCheck
        self.checked = {"table": table, "servings": servings, "text": text, "pdf": pdf}
        self.checks.append(self.checked)
        if isinstance(self.check, Exception):
            raise self.check
        return self._ret(self.check or TableCheck(table=table, doubts=[]))

    def open_batches(self):
        return []

    def guess_product(self, info, generics, locations, image=None):
        return self._ret(self.guess)

    def guess_conversion(self, product_name, from_unit, to_unit):
        return self._ret(self.conversion)

    def guess_measure(self, name, stock_unit):
        from app.ai import AIError
        if self.mode_ != "free":
            self.measures.append((name, stock_unit))
        if self.measure is None and self.mode_ != "free" and not self.error:
            raise AIError("no measure")
        return self._ret(self.measure)

    def read_walmart(self, name, stock_unit, text):
        from app.ai import AIError
        if self.mode_ != "free":
            self.walmart_reads.append((name, stock_unit, text))
        if self.walmart is None and self.mode_ != "free" and not self.error:
            raise AIError("no read")
        return self._ret(self.walmart)

    def match_ingredients(self, names, products):
        return self._ret(self.picks)

    def suggest_week(self, context):
        return self._ret(self.week)


class FakeHA:
    """In-memory Home Assistant. down=True makes every call raise HAError."""

    def __init__(self):
        self.persons_map: dict[str, str] = {}
        self.trackers: dict[str, list[str]] = {}  # person entity -> device_tracker entities
        self.states: dict[str, str] = {}
        self.services: list[str] = []
        self.sent: list[dict] = []
        self.cleared: list[tuple] = []
        self.pushed: dict[str, dict] = {}  # entity id -> {"state", "attributes"} set through the REST API
        self.down = False

    def _check(self):
        if self.down:
            from app.ha import HAError
            raise HAError("down")

    def persons(self):
        self._check()
        return dict(self.persons_map)

    def person_trackers(self):
        self._check()
        return {p: list(self.trackers.get(p, [])) for p in self.persons_map}

    def home_states(self, timeout=None):
        self._check()
        return dict(self.states)

    def notify_services(self):
        self._check()
        return list(self.services)

    def notify(self, service, title, message, tag, actions):
        self._check()
        self.sent.append({"service": service, "title": title, "message": message, "tag": tag, "actions": actions})

    def clear(self, service, tag):
        self._check()
        self.cleared.append((service, tag))

    def set_state(self, entity_id, state, attributes):
        self._check()
        self.pushed[entity_id] = {"state": str(state), "attributes": attributes}
