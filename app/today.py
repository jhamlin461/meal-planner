"""Today: what needs this person now, tonight's dinner, and what's coming up in the next few days."""
from dataclasses import replace
from datetime import date, datetime, time
from typing import Literal

from app import reminders as rm
from app.ha import HAError
from app.house import HouseStore, Person
from app.mealplan import LEFTOVER_PREFIX, week_view

HA_WARNING = "Phone reminders aren't reaching Home Assistant"


def _clock(t: datetime | time) -> str:
    """7 PM, 7:30 PM."""
    return f"{t.hour % 12 or 12}{f':{t.minute:02}' if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def _day(d: date, today: date) -> str:
    """today, tomorrow, yesterday, a weekday within the week either way, else the date."""
    n = (d - today).days
    if -1 <= n <= 1:
        return ("yesterday", "today", "tomorrow")[n + 1]
    return f"{d:%A}" if abs(n) < 7 else f"{d:%B} {d.day}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _mine(it: rm.Item, who, home: dict[str, str] | None) -> bool:
    """Kitchen sees everything; a person sees theirs, plus whoever-is-home items while home (or when HA can't say)."""
    if who == "kitchen" or it.audience == f"person:{who.id}":
        return True
    return it.audience == "home" and (home is None or home.get(who.ha_person) == "home")


def _context(it: rm.Item, who, names: dict[str, str], washer_at: str | None, today: date) -> str:
    """One sentence: when the washer finished, or whose it is and how late."""
    d, chore = it.due.date(), it.key.startswith("chore:")
    late = f"overdue since {_day(d, today)}" if d < today else None
    if washer_at:
        day = "" if d == today else _day(d, today) + " "
        return f"The washer finished {day}at {_clock(time.fromisoformat(washer_at))}."
    if who != "kitchen" and it.audience != "home":  # this device's own item
        return _cap(late) + "." if late else "Your turn today." if chore else "Due today."
    name = names.get(it.audience)
    if name and chore:
        return f"{name}'s turn, {late}." if late else f"{name}'s turn today."
    whose = f"For {name}" if name else "Whoever is home"
    return f"{whose}, {late or 'due today'}."


def _food(vol: dict, names: dict[int, str], today: date) -> list[tuple[datetime, dict]]:
    out = {}
    for key in ("expired_products", "overdue_products", "due_products"):
        for r in vol.get(key) or []:
            pid = int(r["product_id"])
            try:
                d = date.fromisoformat(str(r.get("best_before_date"))[:10])
            except ValueError:
                d = today
            n = (d - today).days
            when = "Past its date" if n < 0 else "Use today" if n == 0 else f"Use by {_day(d, today)}"
            out.setdefault(pid, (datetime.combine(d, time()), {"title": names.get(pid, f"product {pid}"),
                                                                "when": _cap(when), "soon": True,
                                                                "icon": "fridge-outline"}))
    return list(out.values())


def _running_low(vol: dict, names: dict[int, str]) -> list[dict]:
    """Below keep-at-least, straight from Grocy: Today reads, it never rebuilds the Running low list."""
    return [{"title": names.get(int(r["id"])) or r.get("name") or "(item)", "when": "Running low", "soon": False,
             "icon": "basket-outline"} for r in vol.get("missing_products") or []]


def _tonight(grocy, today: date) -> dict | None:
    days = week_view(grocy, today)  # seven days from any start day: [0] is today, [1] tomorrow
    rows = sorted((r for r in days[0].dinner if r.get("type") == "recipe" or str(r.get("note") or "").strip()),
                  key=lambda r: r.get("type") != "recipe")
    if not rows:
        return None
    r = rows[0]
    if r.get("type") != "recipe":
        return {"name": r["note"].strip(), "servings": None, "leftovers": None}
    name = (grocy.get_object("recipes", int(r["recipe_id"])) or {}).get("name") or "A recipe that was deleted"
    left = any(n.get("note") == LEFTOVER_PREFIX + name for n in days[1].lunch)
    return {"name": name, "servings": f"{float(r.get('recipe_servings') or 0):g}" if r.get("recipe_servings") else None,
            "leftovers": f"Leftovers become {days[1].day:%A}'s lunch" if left else None}


def today_view(grocy, house: HouseStore, who: Person | Literal["kitchen"], now: datetime, *, ha=None,
               warn_days: int = 3) -> dict:
    today, ha_down = now.date(), False
    try:
        home = ha.home_states(timeout=2) if ha is not None else None  # a hung HA must not stall the home screen
    except HAError:
        home, ha_down = None, True
    people = {f"person:{p.id}": p.name for p in house.people()}
    laundry = house.flag("laundry")
    now_items, later = [], []  # later: (when it comes back, "tomorrow at 7 PM", item)
    for it in sorted(rm.items(grocy, house, now), key=lambda i: (i.fire_at, i.title)):
        r = house.reminder(it.key)
        until, fired = r.snooze_until, it.fire_at <= now
        snoozed = fired and until is not None and now < until
        if fired and not snoozed and _mine(it, who, home):
            washer_at = r.remind_at if it.key == laundry else None
            now_items.append(replace(it, context=_context(it, who, people, washer_at, today)))
        elif fired and not snoozed:
            if it.audience == "home":  # this person is out: it waits for whoever is home
                later.append((it.fire_at, "waiting at home", it))
        elif _mine(it, who, None):  # whoever is home later may be you
            at = until if snoozed else it.fire_at
            when = f"snoozed until {_clock(at)}" if snoozed else f"{_day(at.date(), today)} at {_clock(at)}"
            later.append((at, when, it))
    later.sort(key=lambda x: x[0])
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    vol = grocy.volatile(warn_days)
    coming = [(at, {"title": it.title, "when": _cap(when), "soon": at.date() <= today, "icon": it.icon})
              for at, when, it in later] + _food(vol, names, today)
    coming = [c for _, c in sorted(coming, key=lambda c: c[0])] + _running_low(vol, names)
    if now_items:
        note = "For everyone" if who == "kitchen" else f"For {who.name}" if home is None else \
            f"{who.name}, you're {'home' if home.get(who.ha_person) == 'home' else 'out'}"
    elif later:
        note = f"Next up: {later[0][2].title}, {later[0][1]}"
    else:
        note = "Nothing else is due in the next few days"
    return {"now_items": now_items, "tonight": _tonight(grocy, today), "coming": coming, "note": note,
            "ha_error": HA_WARNING if ha_down or house.flag("ha_error") else None}
