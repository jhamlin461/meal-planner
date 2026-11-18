"""Today: what needs this person now, tonight's dinner, and what's coming up in the next few days."""
from datetime import date, datetime, time, timedelta
from typing import Literal

from app import reminders as rm
from app.ha import HAError
from app.house import HouseStore, Person
from app.mealplan import LEFTOVER_PREFIX, week_view
from app.setup_grocy import ids

HA_WARNING = "Phone reminders aren't reaching Home Assistant"


def _clock(t: datetime | time) -> str:
    """7 PM, 7:30 PM."""
    return f"{t.hour % 12 or 12}{f':{t.minute:02}' if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def _day(d: date, today: date) -> str:
    n = (d - today).days
    return "today" if n == 0 else "tomorrow" if n == 1 else f"{d:%A}"  # within the week, so the weekday is enough


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _mine(it: rm.Item, who, home: dict[str, str] | None) -> bool:
    """Kitchen sees everything; a person sees theirs, plus whoever-is-home items while home (or when HA can't say)."""
    if who == "kitchen" or it.audience == f"person:{who.id}":
        return True
    return it.audience == "home" and (home is None or home.get(who.ha_person) == "home")


def _food(grocy, warn_days: int, today: date) -> list[tuple[datetime, dict]]:
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    vol, out = grocy.volatile(warn_days), {}
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
                                                                "when": _cap(when), "soon": True}))
    return list(out.values())


def _running_low(grocy) -> list[dict]:
    names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
    rows = grocy.objects("shopping_list", shopping_list_id=ids(grocy).list["Running low"])
    return [{"title": names.get(int(r.get("product_id") or 0)) or r.get("note") or "(item)",
             "when": "Running low", "soon": False} for r in rows if not int(float(r.get("done") or 0))]


def _tonight(grocy, today: date) -> dict | None:
    days = week_view(grocy, today)  # seven days from any start day: [0] is today, [1] tomorrow
    rows = sorted(days[0].dinner, key=lambda r: r.get("type") != "recipe")
    if not rows:
        return None
    r = rows[0]
    if r.get("type") != "recipe":
        return {"name": r.get("note") or "", "servings": None, "leftovers": None}
    name = (grocy.get_object("recipes", int(r["recipe_id"])) or {}).get("name", "")
    left = any(n.get("note") == LEFTOVER_PREFIX + name for n in days[1].lunch)
    return {"name": name, "servings": f"{float(r.get('recipe_servings') or 0):g}" if r.get("recipe_servings") else None,
            "leftovers": f"Leftovers become {days[1].day:%A}'s lunch" if left else None}


def today_view(grocy, house: HouseStore, who: Person | Literal["kitchen"], now: datetime, *, ha=None,
               warn_days: int = 3) -> dict:
    today, ha_down = now.date(), False
    try:
        home = ha.home_states() if ha is not None else None
    except HAError:
        home, ha_down = None, True
    now_items, later = [], []  # later: (when it comes back, "tomorrow at 7 PM", title)
    for it in sorted(rm.items(grocy, house, now), key=lambda i: (i.fire_at, i.title)):
        until = house.reminder(it.key).snooze_until
        snoozed = it.fire_at <= now and until is not None and now < until
        if not snoozed and it.fire_at <= now:
            if _mine(it, who, home):
                now_items.append(it)
        elif _mine(it, who, None):  # whoever is home later may be you
            at = until if snoozed else it.fire_at
            later.append((at, f"snoozed until {_clock(at)}" if snoozed else f"{_day(at.date(), today)} at {_clock(at)}",
                          it.title))
    later.sort(key=lambda x: x[0])
    coming = [(at, {"title": title, "when": _cap(when), "soon": at.date() == today}) for at, when, title in later]
    coming = [c for _, c in sorted(coming + _food(grocy, warn_days, today), key=lambda c: c[0])] + _running_low(grocy)
    if now_items:
        note = "For everyone" if who == "kitchen" else f"For {who.name}" if home is None else \
            f"{who.name}, you're {'home' if home.get(who.ha_person) == 'home' else 'out'}"
    elif later:
        note = f"Next up: {later[0][2]}, {later[0][1]}"
    else:
        note = "Nothing else is due in the next few days"
    return {"now_items": now_items, "tonight": _tonight(grocy, today), "coming": coming, "note": note,
            "ha_error": HA_WARNING if ha_down or house.flag("ha_error") else None}
