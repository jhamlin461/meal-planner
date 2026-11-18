"""Reminder engine: what is due, who hears about it, and the loop that sends and clears phone notifications."""
import logging
import re
import threading
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.ha import HAError
from app.house import LOCK, HouseStore, Person, Reminder, by_grocy_user, gid

log = logging.getLogger(__name__)
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


def open_items(grocy) -> dict[str, tuple[str, datetime | None, object]]:
    """Every open chore, task and battery: key -> (title, due or None, assigned Grocy user id or None)."""
    out = {f"chore:{c['chore_id']}": (c.get("chore_name") or "", _due(c.get("next_estimated_execution_time")),
                                      c.get("next_execution_assigned_to_user_id")) for c in grocy.chores_status()}
    out |= {f"task:{t['id']}": (t.get("name") or "", _due(t.get("due_date")), t.get("assigned_to_user_id"))
            for t in grocy.open_tasks() if not int(t.get("done") or 0)}
    names = {gid(b["id"]): b.get("name") or "" for b in grocy.objects("batteries")}
    out |= {f"battery:{b['battery_id']}": (names.get(gid(b["battery_id"]), ""), _due(b.get("next_estimated_charge_time")), None)
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
        p = by_grocy_user(people, uid)
        out.append(Item(key, title, _when(due.date(), now.date()), due, fire, r.audience or (f"person:{p.id}" if p else "home"),
                        r.verb or "Done", r.repeat_min or 120, _icon(key.split(":")[0], title)))
    return out


def items(grocy, house: HouseStore, now: datetime) -> list[Item]:
    """Open chores, tasks and upkeep due by the end of the day three days from now."""
    return _items(open_items(grocy), house, now)


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
        open_ = open_items(grocy)
        try:
            home = ha.home_states()  # HA unreachable as a whole: nothing else can work this tick
        except HAError as e:
            return _flag(house, [e])
        errors = _clear_finished(house, ha, keys, open_)
        if not in_quiet(now, settings.quiet_hours):
            errors += _send_due(house, ha, open_, home, now)
        _flag(house, errors)


def _flag(house: HouseStore, errors: list[HAError]) -> None:
    for e in errors:
        log.warning("Home Assistant: %s", e)
    house.set_flag("ha_error", f"Home Assistant unreachable: {errors[0]}"[:200] if errors else None)


def _clear_finished(house: HouseStore, ha, keys: list[str], open_: dict) -> list[HAError]:
    """Deleted or completed: clear every phone and forget it. Recurring and moved on: clear every phone, keep settings."""
    errors = []
    for key in keys:
        r = house.reminder(key)
        due = open_[key][1] if key in open_ else None
        if key in open_ and r.sent_for in (None, due and due.isoformat()):
            continue
        errors += _clear(house, ha, r, drop=key not in open_)
    return errors


def _clear_phones(ha, r: Reminder) -> tuple[list[str], list[HAError]]:
    """Clear r's notification on every phone it reached; returns the phones to retry. A phone HA no longer knows (4xx)
    is forgotten; one HA couldn't reach is kept, so the clear is retried."""
    retry, errors = [], []
    for service in r.sent_to:
        try:
            if ha is None:
                raise HAError("Home Assistant is not set up")
            ha.clear(service, r.key)
        except HAError as e:
            errors.append(e)
            if not (e.status and 400 <= e.status < 500):
                retry.append(service)
    return retry, errors


def _clear(house: HouseStore, ha, r: Reminder, drop: bool) -> list[HAError]:
    """Clear every phone, then reset the sent state (settings stay) or, with drop, forget the reminder."""
    retry, errors = _clear_phones(ha, r)
    # sent_for stays until every phone is cleared, so the next tick retries the clear
    reset = Reminder(r.key, r.audience, r.remind_at, r.repeat_min, sent_to=retry, verb=r.verb,
                     sent_for=r.sent_for if retry else None)
    if drop and not retry:
        house.drop_reminder(r.key)
    elif reset != r:  # unchanged (e.g. an item that never had a reminder row): no blank row is written
        house.save_reminder(reset)
    return errors


def _send_due(house: HouseStore, ha, open_: dict, home: dict[str, str], now: datetime) -> list[HAError]:
    """Send each fired item to its recipients; a phone that fails doesn't hold up the others or later items.
    Between repeats, only recipients not reached yet (just home, or their phone failed) are tried."""
    people, errors = house.people(), []
    for it in _items(open_, house, now):
        r = house.reminder(it.key)
        snoozed, snooze_over = r.snooze_until is not None and now < r.snooze_until, r.snooze_until is not None
        if it.fire_at > now or snoozed:
            continue
        again = snooze_over or r.last_sent is None or now - r.last_sent >= timedelta(minutes=it.repeat_min)
        to = [p for p in recipients(it, people, home) if again or p.notify_service not in r.sent_to]
        if not to:
            continue  # nobody (new) to tell: held until someone comes home
        kind, num = it.key.split(":")
        reached = False
        for p in to:
            try:
                ha.notify(p.notify_service, it.title, it.context, it.key,
                          [(f"HM:done:{kind}:{num}:{p.id}", it.verb), (f"HM:snooze:{kind}:{num}:{p.id}", "Snooze 15 min")])
            except HAError as e:
                errors.append(e)
                continue
            reached = True
            if p.notify_service not in r.sent_to:
                r.sent_to.append(p.notify_service)
        if reached:  # nobody reached: everyone is retried next tick
            r.sent_for = it.due.isoformat()
            if again:
                r.last_sent, r.snooze_until = now, None
            house.save_reminder(r)
    return errors


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


# ---- Done and Snooze, from a phone or the app -----------------------------------------------------------------------
ACTION = re.compile(r"HM:(done|snooze):(chore|task|battery):([0-9]{1,9}):([0-9]{1,9})")


def _report(house: HouseStore, errors: list[HAError]) -> None:
    if errors:
        _flag(house, errors)  # the next tick clears it once HA answers again


def _current(open_: dict, key: str, due: str | None) -> bool:
    """`due` (the ISO date a button or notification was for) is still the item's open occurrence. None or garbage
    (an old cached page, a forged form) never is. An open task with no due date has only the one occurrence; a chore
    with none (scheduled manually) has nothing to tell one Done from the next, so it is never current."""
    if key not in open_:
        return False
    current = open_[key][1]
    if current is None:
        return key.startswith("task:")
    try:
        return due is not None and due.isascii() and _due(due) == current
    except ValueError:
        return False


def complete(grocy, house: HouseStore, ha, key: str, person: Person | None, due: str | None,
             open_: dict | None = None, skipped: bool = False) -> bool:
    """Done for the occurrence due on `due`: run the chore (credited to `person`) / complete the task / charge the
    upkeep item, and clear it on every phone. When that occurrence is already done (the other phone, the app, Grocy)
    nothing runs and nothing is cleared (the tick clears phones told about a finished occurrence), unless the item
    is gone. A recurring item keeps its settings; a done task's reminder is dropped.
    `open_` is open_items(grocy) read under the caller's LOCK; `skipped` skips a chore instead. True when it ran."""
    kind, num = key.split(":")
    with LOCK:
        open_ = open_items(grocy) if open_ is None else open_
        run = _current(open_, key, due)
        if run:
            if kind == "chore":
                grocy.execute_chore(int(num), person.grocy_user_id if person else None, skipped)
            elif kind == "task":
                grocy.complete_task(int(num))
            else:
                grocy.charge_battery(int(num))
        if run or key not in open_:  # a stale Done leaves the open occurrence's phones and snooze to the tick
            _report(house, _clear(house, ha, house.reminder(key), drop=key not in open_ or (run and kind == "task")))
        return run


def snooze(house: HouseStore, ha, key: str, now: datetime, minutes: int = 15) -> None:
    with LOCK:
        r = house.reminder(key)
        r.sent_to, errors = _clear_phones(ha, r)
        r.snooze_until = now + timedelta(minutes=minutes)
        house.save_reminder(r)
    _report(house, errors)


def action(grocy, house: HouseStore, ha, action_id: str, now: datetime) -> str | None:
    """A phone's Done or Snooze button (`HM:<done|snooze>:<kind>:<id>:<person id>`). It acts only on the occurrence
    that phone was told about (sent_for): if someone already did it, it only clears the phones.
    None for an id that isn't ours, "unknown-person" for a person id the house doesn't have."""
    m = ACTION.fullmatch(action_id)
    if not m:
        return None
    verb, kind, num, pid = m.groups()
    person, key = house.person(int(pid)), f"{kind}:{num}"
    if person is None:
        return "unknown-person"
    with LOCK:
        r, open_ = house.reminder(key), open_items(grocy)
        if r.sent_for is None or not _current(open_, key, r.sent_for):
            _report(house, _clear(house, ha, r, drop=key not in open_))
            return "already-done"
        if verb == "done":
            complete(grocy, house, ha, key, person, r.sent_for, open_)
            return "done"
        snooze(house, ha, key, now)
        return "snoozed"


# ---- laundry ----------------------------------------------------------------------------------------------------------
LAUNDRY = "Move the laundry"


def washer_finished(grocy, house: HouseStore, now: datetime) -> str:
    """One open "Move the laundry" task at a time, reminding whoever is home every 15 minutes from now."""
    with LOCK:
        key = house.flag("laundry")
        if key and key in open_items(grocy):
            return "already-open"
        cat = next((c["id"] for c in grocy.objects("task_categories") if c["name"] == "Household"), None)
        tid = grocy.create("tasks", {"name": LAUNDRY, "description": "", "due_date": now.date().isoformat(),
                                     "assigned_to_user_id": None, "category_id": cat})
        house.save_reminder(Reminder(f"task:{tid}", "home", now.strftime("%H:%M"), 15, verb="Moved it"))
        house.set_flag("laundry", f"task:{tid}")
        return "created"


def washer_started(grocy, house: HouseStore, ha) -> str:
    """A new load means the last one was moved: complete its task (crediting nobody) and clear the phones."""
    with LOCK:
        key, open_ = house.flag("laundry"), open_items(grocy)
        house.set_flag("laundry", None)
        if key not in open_:
            return "none-open"
        due = open_[key][1]
        return "cleared" if complete(grocy, house, ha, key, None, due.isoformat() if due else "", open_) else "none-open"
