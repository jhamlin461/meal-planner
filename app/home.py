"""Home pages: turn chore, task and upkeep forms into Grocy rows and reminder settings, and back again."""
import re
from datetime import date

from app.house import LOCK, HouseStore, Person, Reminder

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SUPPLIES = "Household supplies"

STARTER_CHORES = [
    {"name": "Scoop the litter boxes", "fields": {"period_type": "daily", "period_interval": 1},
     "remind_at": "18:00", "rotate": True},
    {"name": "Change the litter", "fields": {"period_type": "dynamic-regular", "period_interval": 14, "period_days": 14},
     "remind_at": "09:00", "rotate": True, "supply": ("litter",), "amount": 1},
    {"name": "Take out the trash", "fields": {"period_type": "weekly", "period_config": "sunday"},
     "remind_at": "19:00", "rotate": True, "supply": ("garbage bag", "trash bag"), "amount": 1},
]


def _pos(form, key: str, what: str) -> int:
    s = str(form.get(key) or "").strip()
    if not s.isdecimal() or int(s) < 1:
        raise ValueError(f"{what} must be a whole number above 0")
    return int(s)


def _time(form) -> str:
    s = str(form.get("remind_at") or "").strip() or "09:00"
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise ValueError("Remind at must be a time like 09:00")
    return s


def _repeat(form) -> int | None:
    return _pos(form, "repeat_min", "Repeat every") if str(form.get("repeat_min") or "").strip() else None


def _name(form, key: str, what: str) -> str:
    n = str(form.get(key) or "").strip()
    if not n:
        raise ValueError(f"{what} can't be blank")
    return n


def _person(people: list[Person], v) -> Person | None:
    return next((p for p in people if str(p.id) == str(v)), None)


def rotation(people: list[Person]) -> dict:
    ids = [str(p.grocy_user_id) for p in people if isinstance(p.grocy_user_id, int) and p.grocy_user_id > 0]
    if not ids:
        raise ValueError("Add people on the People page first, or pick one person.")
    return {"assignment_type": "who-least-did-first", "assignment_config": ",".join(ids)}


def chore_fields(form, people: list[Person]) -> tuple[dict, dict]:
    """Grocy chore row and Reminder fields from the chore form; ValueError (with a message to show) when it is bad."""
    row = {"name": _name(form, "name", "Name"), "track_date_only": 1}
    sched = form.get("schedule") or "daily"
    # Grocy's update merges fields, so every period field is set: the ones that don't apply are blanked
    row |= {"period_interval": 1, "period_days": None, "period_config": ""}
    if sched == "daily":
        row |= {"period_type": "daily"}
    elif sched == "every":  # N days after it was last done
        n = _pos(form, "every", "Days between")
        row |= {"period_type": "dynamic-regular", "period_interval": n, "period_days": n}
    elif sched == "weekly":
        days = [d for d in DAYS if d in form.getlist("days")]
        if not days:
            raise ValueError("Pick at least one day of the week")
        row |= {"period_type": "weekly", "period_config": ",".join(days)}
    elif sched == "monthly":
        day = _pos(form, "monthly", "Day of the month")
        if day > 31:
            raise ValueError("Day of the month must be 1 to 31")
        row |= {"period_type": "monthly", "period_days": day}
    else:
        raise ValueError("Pick how often it repeats")
    who = form.get("who") or "turns"
    if who == "turns":
        row |= rotation(people)
    else:
        p = _person(people, who)
        if p is None:
            raise ValueError("Pick who does it")
        row |= {"assignment_type": "no-assignment", "next_execution_assigned_to_user_id": p.grocy_user_id}
    pid = str(form.get("product_id") or "").strip()
    if pid:
        amt = str(form.get("product_amount") or "").strip() or "1"
        try:
            amount = float(amt)
        except ValueError:
            amount = 0
        if not amount > 0 or amount == float("inf"):
            raise ValueError("Amount must be a number above 0")
        row |= {"consume_product_on_chore_execution": 1, "product_id": int(pid), "product_amount": amount}
    else:
        row |= {"consume_product_on_chore_execution": 0, "product_id": None}
    return row, {"audience": None, "remind_at": _time(form), "repeat_min": _repeat(form)}


def task_fields(form, people: list[Person]) -> tuple[dict, dict]:
    row = {"name": _name(form, "title", "Title"), "description": str(form.get("note") or "").strip(),
           "due_date": str(form.get("due") or "").strip() or None, "assigned_to_user_id": None}
    if row["due_date"]:
        try:
            date.fromisoformat(row["due_date"])
        except ValueError:
            raise ValueError("Due date must be a date") from None
    who = form.get("who") or "home"
    if who == "home":
        audience = "home"
    else:
        p = _person(people, who)
        if p is None:
            raise ValueError("Pick who it is for")
        audience, row["assigned_to_user_id"] = f"person:{p.id}", p.grocy_user_id
    return row, {"audience": audience, "remind_at": _time(form), "repeat_min": None}


def upkeep_fields(form) -> tuple[dict, dict]:
    row = {"name": _name(form, "name", "Name"), "charge_interval_days": _pos(form, "every", "Days between"),
           "used_in": ""}
    return row, {"audience": None, "remind_at": _time(form), "repeat_min": None}


def save(grocy, house: HouseStore, entity: str, kind: str, row: dict, rem: dict, id: int | None = None) -> int:
    """Create or update the Grocy row and keep its reminder settings (snooze and sent state survive an edit)."""
    if id is None:
        id = grocy.create(entity, row)
    else:
        grocy.update(entity, id, row)
    with LOCK:
        r = house.reminder(f"{kind}:{id}")
        for k, v in rem.items():
            setattr(r, k, v)
        house.save_reminder(r)
    return id


def remove(grocy, house: HouseStore, entity: str, kind: str, id: int) -> None:
    grocy.delete(entity, id)  # the reminder loop clears every phone it reached, then drops the reminder


def supplies(grocy) -> list[dict]:
    group = next((g["id"] for g in grocy.objects("product_groups") if g["name"] == SUPPLIES), None)
    return [p for p in grocy.objects("products") if group is not None and str(p.get("product_group_id")) == str(group)]


def add_starters(grocy, house: HouseStore, people: list[Person]) -> int:
    have = {c["name"] for c in grocy.objects("chores")}
    products = grocy.objects("products")
    added = 0
    for s in STARTER_CHORES:
        if s["name"] in have:
            continue
        row = {"name": s["name"], "track_date_only": 1, **s["fields"], "consume_product_on_chore_execution": 0,
               "assignment_type": "no-assignment"}
        if s["rotate"] and people:
            row |= rotation(people)
        match = next((p for p in products if any(w in p["name"].lower() for w in s.get("supply", ()))), None)
        if match:
            row |= {"consume_product_on_chore_execution": 1, "product_id": match["id"], "product_amount": s["amount"]}
        save(grocy, house, "chores", "chore", row, {"audience": None, "remind_at": s["remind_at"], "repeat_min": None})
        added += 1
    return added


# ---- reading back for the pages -----------------------------------------------------------------------------------
def _due(s: str | None, today: date) -> dict:
    d = date.fromisoformat(s[:10]) if s else None
    return {"date": d, "overdue": bool(d and d < today), "today": d == today}


def chore_rows(grocy, people: list[Person], today: date) -> list[dict]:
    c_by_id = {c["id"]: c for c in grocy.objects("chores")}
    names = {i: c.get("name") for i, c in c_by_id.items()}
    out = []
    for r in grocy.chores_status():
        p = next((p for p in people if p.grocy_user_id == r.get("next_execution_assigned_to_user_id")), None)
        turns = c_by_id.get(r["chore_id"], {}).get("assignment_type") == "who-least-did-first"
        out.append({"id": r["chore_id"], "turns": turns, "name": names.get(r["chore_id"]) or r.get("chore_name") or "",
                    "who": p.name if p else None, **_due(r.get("next_estimated_execution_time"), today)})
    return sorted(out, key=lambda x: (x["date"] is None, x["date"] or today, x["name"]))


def task_rows(grocy, people: list[Person], today: date) -> list[dict]:
    out = []
    for t in grocy.open_tasks():
        p = next((p for p in people if p.grocy_user_id == t.get("assigned_to_user_id")), None)
        out.append({"id": t["id"], "name": t.get("name") or "", "who": p.name if p else None, **_due(t.get("due_date"), today)})
    return sorted(out, key=lambda x: (x["date"] is None, x["date"] or today, x["name"]))


def upkeep_rows(grocy, today: date) -> list[dict]:
    names = {b["id"]: b.get("name") or "" for b in grocy.objects("batteries")}
    out = [{"id": r["battery_id"], "name": names.get(r["battery_id"], ""), "who": None,
            **_due(r.get("next_estimated_charge_time"), today)} for r in grocy.batteries_status()]
    return sorted(out, key=lambda x: (x["date"] is None, x["date"] or today, x["name"]))


def chore_values(c: dict, rem: Reminder, people: list[Person]) -> dict:
    """Form values for editing an existing chore."""
    pt = c.get("period_type")
    v = {"name": c.get("name", ""), "schedule": "daily", "every": "", "days": [], "monthly": "",
         "who": "turns", "remind_at": rem.remind_at, "repeat_min": rem.repeat_min or "",
         "product_id": str(c.get("product_id") or ""), "product_amount": c.get("product_amount") or 1}
    if pt == "weekly":
        v |= {"schedule": "weekly", "days": str(c.get("period_config") or "").split(",")}
    elif pt == "monthly":
        v |= {"schedule": "monthly", "monthly": c.get("period_days") or ""}
    elif pt == "dynamic-regular" or (pt == "daily" and int(c.get("period_interval") or 1) > 1):
        v |= {"schedule": "every", "every": c.get("period_days") or c.get("period_interval") or ""}
    if c.get("assignment_type") != "who-least-did-first":
        p = next((p for p in people if p.grocy_user_id == c.get("next_execution_assigned_to_user_id")), None)
        v["who"] = str(p.id) if p else "turns"
    return v
