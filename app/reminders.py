"""Reminder engine: what is due, who hears about it, and the loop that sends and clears phone notifications."""
import logging
import threading
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.ha import HAError
from app.house import HouseStore, Person, Reminder

log = logging.getLogger(__name__)
LOCK = threading.Lock()  # one reminder change at a time: the loop, and (Task 7) Done / Snooze
ICONS = {"chore": "broom", "task": "calendar-check", "battery": "battery-sync-outline"}
NAMED_ICONS = (("litter", "cat"), ("trash", "trash-can-outline"), ("laundry", "washing-machine"))


@dataclass
class Item:
    key: str
    title: str
    context: str
    due: datetime
    fire_at: datetime
    audience: str
    verb: str
    repeat_min: int
    icon: str


def _due(s) -> datetime | None:
    """Grocy's date or date-time, as the start of that day (everything here is reminded by the day)."""
    return datetime.combine(date.fromisoformat(str(s)[:10]), time()) if s else None


def _open(grocy) -> dict[str, tuple[str, datetime | None, object]]:
    """Every open chore, task and battery: key -> (title, due or None, assigned Grocy user id or None)."""
    out = {f"chore:{c['chore_id']}": (c.get("chore_name") or "", _due(c.get("next_estimated_execution_time")),
                                      c.get("next_execution_assigned_to_user_id")) for c in grocy.chores_status()}
    out |= {f"task:{t['id']}": (t.get("name") or "", _due(t.get("due_date")), t.get("assigned_to_user_id"))
            for t in grocy.open_tasks() if not int(t.get("done") or 0)}
    names = {b["id"]: b.get("name") or "" for b in grocy.objects("batteries")}
    out |= {f"battery:{b['battery_id']}": (names.get(b["battery_id"], ""), _due(b.get("next_estimated_charge_time")), None)
            for b in grocy.batteries_status()}
    return out


def _when(due: date, today: date) -> str:
    n = (due - today).days
    if n < 0:
        return f"Overdue since {due:%a %b} {due.day}"
    return "Due today" if n == 0 else "Due tomorrow" if n == 1 else f"Due {due:%A}"


def _icon(kind: str, title: str) -> str:
    return next((i for word, i in NAMED_ICONS if word in title.lower()), ICONS[kind])


def _items(open_: dict, house: HouseStore, now: datetime) -> list[Item]:
    people, last = house.people(), now.date() + timedelta(days=3)
    out = []
    for key, (title, due, uid) in open_.items():
        if due is None or due.date() > last:
            continue
        r = house.reminder(key)
        fire = due if due.date() < now.date() else datetime.combine(due.date(), time.fromisoformat(r.remind_at))
        p = next((p for p in people if uid is not None and str(p.grocy_user_id) == str(uid)), None)
        out.append(Item(key, title, _when(due.date(), now.date()), due, fire, r.audience or (f"person:{p.id}" if p else "home"),
                        r.verb or "Done", r.repeat_min or 120, _icon(key.split(":")[0], title)))
    return out


def items(grocy, house: HouseStore, now: datetime) -> list[Item]:
    """Open chores, tasks and upkeep due by the end of the day three days from now."""
    return _items(_open(grocy), house, now)


def recipients(item: Item, people: list[Person], home_states: dict[str, str]) -> list[Person]:
    if item.audience.startswith("person:"):
        to = [p for p in people if f"person:{p.id}" == item.audience]  # even when away
    else:
        to = [p for p in people if home_states.get(p.ha_person) == "home"]
    return [p for p in to if p.notify_service]


def in_quiet(now: datetime, quiet: tuple[time, time]) -> bool:
    start, end = quiet
    t = now.time()
    return start <= t < end if start <= end else t >= start or t < end


def tick(grocy, house: HouseStore, ha, settings, now: datetime) -> None:
    with LOCK:
        keys = house.keys()  # before Grocy: a reminder saved after this read can't be mistaken for a deleted item
        open_ = _open(grocy)
        try:
            _clear_finished(house, ha, keys, open_)
            if in_quiet(now, settings.quiet_hours):
                return
            home = ha.home_states()
            house.set_flag("ha_error", None)
            _send_due(house, ha, open_, home, now)
        except HAError as e:
            log.warning("Home Assistant unreachable: %s", e)
            house.set_flag("ha_error", f"Home Assistant unreachable: {e}"[:200])


def _clear_finished(house: HouseStore, ha, keys: list[str], open_: dict) -> None:
    """Deleted or completed: clear every phone and forget it. Recurring and moved on: clear every phone, keep settings."""
    for key in keys:
        r = house.reminder(key)
        due = open_[key][1] if key in open_ else None
        if key in open_ and r.sent_for in (None, due and due.isoformat()):
            continue
        for service in r.sent_to:
            ha.clear(service, key)  # HAError leaves sent_to as is, so the next tick retries
        if key in open_:
            house.save_reminder(Reminder(key, r.audience, r.remind_at, r.repeat_min, verb=r.verb))
        else:
            house.drop_reminder(key)


def _send_due(house: HouseStore, ha, open_: dict, home: dict[str, str], now: datetime) -> None:
    people = house.people()
    for it in _items(open_, house, now):
        r = house.reminder(it.key)
        snoozed, snooze_over = r.snooze_until is not None and now < r.snooze_until, r.snooze_until is not None
        if it.fire_at > now or snoozed:
            continue
        if not snooze_over and r.last_sent and now - r.last_sent < timedelta(minutes=it.repeat_min):
            continue
        to = recipients(it, people, home)
        if not to:
            continue  # nobody to tell yet: held until someone comes home
        kind, id = it.key.split(":")
        r.sent_for = it.due.isoformat()
        for p in to:
            try:
                ha.notify(p.notify_service, it.title, it.context, it.key,
                          [(f"HM:done:{kind}:{id}:{p.id}", it.verb), (f"HM:snooze:{kind}:{id}:{p.id}", "Snooze 15 min")])
            except HAError:
                house.save_reminder(r)  # remember the phones already reached so they get cleared later
                raise
            if p.notify_service not in r.sent_to:
                r.sent_to.append(p.notify_service)
        r.last_sent, r.snooze_until = now, None
        house.save_reminder(r)


def start_loop(app_state_fn, interval: int = 60) -> threading.Thread:
    """Daemon thread: tick(*app_state_fn(), now) every `interval` seconds; failures are logged and the loop goes on."""
    def run():
        while True:
            try:
                tick(*app_state_fn(), datetime.now())
            except Exception:
                log.exception("reminder tick failed")
            clock.sleep(interval)

    t = threading.Thread(target=run, name="reminders", daemon=True)
    t.start()
    return t
