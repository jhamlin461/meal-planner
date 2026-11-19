from dataclasses import replace
from datetime import date, datetime, time, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.house import Reminder
from app.importer import PendingStore
from app import reminders as rm
from app.reminders import tick, washer_finished, washer_started
from app.web import create_app
from tests.fakes import FakeAI
from tests.test_reminders import CAT_PHONE, JOSH_PHONE, at, chore, task, world

NO_QUIET = (time(0), time(0))


def client(tmp_path, g, house, ha):
    s = Settings("", "", "", "KEY", None, 6, tmp_path, 3, quiet_hours=NO_QUIET)
    return TestClient(create_app(g, FakeAI(), None, s, PendingStore(tmp_path / "p"), house=house, ha=ha),
                      follow_redirects=False)


def tap(g, house, ha, action):
    """A phone's Done/Snooze button, as the live HA connection hands it over."""
    return rm.action(g, house, ha, action, datetime.now())


def test_hooks_are_gone(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    for path in ("/api/hook/action", "/api/events/washer-finished", "/api/events/washer-started"):
        assert c.post(path, json={"action": "HM:done:task:1:1"}).status_code in (404, 405), path


def test_rejects_bad_action_ids(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    assert tap(g, house, ha, f"HM:done:task:{tid}:999") == "unknown-person"
    for bad in ("rm -rf", f"HM:done:task:x:{josh}", f"HM:done:robot:{tid}:{josh}", f"HM:done:task:١:{josh}",
                f"HM:done:task:{tid}:١", f"HM:done:task:{'9' * 5000}:{josh}", f"HM:nap:task:{tid}:{josh}"):
        assert tap(g, house, ha, bad) is None, bad
    assert g.open_tasks() and house.reminder(f"task:{tid}").sent_for  # nothing was done


def test_action_done_completes_and_clears(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    assert tap(g, house, ha, f"HM:done:task:{tid}:{cat}") == "done" and g.open_tasks() == []
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()
    # a recurring chore: credited to whoever tapped, phones cleared, sent state reset, settings kept
    ha.cleared.clear()
    cid = chore(g, house, audience="home", repeat_min=30, verb="Scooped")
    tick(g, house, ha, s, at(18, 18))
    tap(g, house, ha, f"HM:done:chore:{cid}:{cat}")
    assert g.chore_log[-1]["done_by"] == 4
    assert sorted(ha.cleared) == [(CAT_PHONE, f"chore:{cid}"), (JOSH_PHONE, f"chore:{cid}")]
    rem = house.reminder(f"chore:{cid}")
    assert (rem.audience, rem.remind_at, rem.repeat_min, rem.verb) == ("home", "18:00", 30, "Scooped")
    assert (rem.sent_to, rem.last_sent, rem.snooze_until, rem.sent_for) == ([], None, None, None)


def trash(g, house, bags=5):
    pid = g.create("products", {"name": "Garbage bags"})
    g.add_stock(pid, bags, None, None)
    cid = g.create("chores", {"name": "Take out the trash", "period_type": "daily", "period_interval": 1,
                              "assignment_type": "no-assignment", "consume_product_on_execution": 1,
                              "product_id": pid, "product_amount": 1})
    house.save_reminder(Reminder(f"chore:{cid}", "home", "19:00"))
    return cid, pid


def test_double_done_executes_once(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    tick(g, house, ha, s, at(18, 19))
    act = f"HM:done:chore:{cid}:{josh}"
    assert tap(g, house, ha, act) == "done"
    assert tap(g, house, ha, act) == "already-done"
    # the other phone, a second later
    tap(g, house, ha, f"HM:done:chore:{cid}:{cat}")
    assert len(g.chore_log) == 1 and g.stock_amounts()[pid] == 4
    # done in the app first, then a stale notification is tapped: no second run
    tick(g, house, ha, s, at(19, 19))
    assert c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-19"}, cookies={"who": str(cat)}).status_code == 303
    assert tap(g, house, ha, act) == "already-done"
    assert len(g.chore_log) == 2 and g.stock_amounts()[pid] == 3


def test_snooze_action(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    before = datetime.now()
    assert tap(g, house, ha, f"HM:snooze:task:{tid}:{josh}") == "snoozed"
    until = house.reminder(f"task:{tid}").snooze_until
    assert before + timedelta(minutes=15) <= until <= datetime.now() + timedelta(minutes=15)
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    sent = len(ha.sent)
    tick(g, house, ha, replace(s, quiet_hours=NO_QUIET), until - timedelta(minutes=1))
    assert len(ha.sent) == sent
    tick(g, house, ha, replace(s, quiet_hours=NO_QUIET), until)
    assert len(ha.sent) == sent + 2 and g.open_tasks()  # snoozing never completes it


def test_washer_finished_creates_one_task(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    hh = g.create("task_categories", {"name": "Household"})
    assert washer_finished(g, house, datetime.now()) == "created"
    assert washer_finished(g, house, datetime.now()) == "already-open"
    (t,) = g.open_tasks()
    assert (t["name"], t["category_id"], t["assigned_to_user_id"]) == ("Move the laundry", hh, None)
    assert t["due_date"] == date.today().isoformat() and house.flag("laundry") == f"task:{t['id']}"
    r = house.reminder(f"task:{t['id']}")
    assert (r.audience, r.repeat_min, r.verb) == ("home", 15, "Moved it")
    # moved by hand (Moved it): the next load's finish makes a new task
    g.complete_task(t["id"])
    assert washer_finished(g, house, datetime.now()) == "created"
    assert len(g.open_tasks()) == 1


def test_washer_started_clears_open_laundry(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    assert washer_started(g, house, ha) == "none-open"
    washer_finished(g, house, at(18, 10, 7))
    tick(g, house, ha, s, at(18, 10, 7))
    tid = int(house.flag("laundry").split(":")[1])
    assert washer_started(g, house, ha) == "cleared"
    assert g.open_tasks() == [] and house.flag("laundry") is None
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()
    assert washer_started(g, house, ha) == "none-open"


def test_today_buttons(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    assert c.post(f"/today/task/{tid}/snooze").status_code == 303
    assert house.reminder(f"task:{tid}").snooze_until and len(ha.cleared) == 2
    assert c.post(f"/today/task/{tid}/done", data={"due": "2026-11-18"}, cookies={"who": str(josh)}).status_code == 303
    assert g.open_tasks() == [] and f"task:{tid}" not in house.keys()
    assert c.post("/today/nope/1/done").status_code == 422


def test_done_while_ha_down_runs_once_and_clears_later(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    cid, pid = trash(g, house)
    tick(g, house, ha, s, at(18, 19))
    ha.down = True
    act = f"HM:done:chore:{cid}:{josh}"
    assert tap(g, house, ha, act) == "done"
    assert tap(g, house, ha, act) == "already-done"
    assert len(g.chore_log) == 1 and g.stock_amounts()[pid] == 4 and house.flag("ha_error")
    assert sorted(house.reminder(f"chore:{cid}").sent_to) == [CAT_PHONE, JOSH_PHONE]  # kept for the retry
    ha.down = False
    tick(g, house, ha, s, at(18, 19, 1))
    assert sorted(ha.cleared) == [(CAT_PHONE, f"chore:{cid}"), (JOSH_PHONE, f"chore:{cid}")]
    assert house.reminder(f"chore:{cid}") == Reminder(f"chore:{cid}", "home", "19:00") and house.flag("ha_error") is None


def test_app_done_for_an_old_occurrence_is_a_no_op(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    assert 'name="due" value="2026-11-18"' in c.get("/home").text  # the button carries the occurrence it shows
    tick(g, house, ha, s, at(18, 19))
    tap(g, house, ha, f"HM:done:chore:{cid}:{josh}")
    # the Home page was loaded before the phone tap: its Done is for the 18th, which is already done
    r = c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-18"}, cookies={"who": str(cat)})
    assert r.status_code == 303 and "Already+done" in r.headers["location"]
    r = c.post(f"/home/chores/{cid}/done", cookies={"who": str(cat)})  # an old cached page with no due field
    assert "Already+done" in r.headers["location"]
    assert len(g.chore_log) == 1 and g.stock_amounts()[pid] == 4
    # the button for the current occurrence still works
    c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-19"}, cookies={"who": str(cat)})
    assert len(g.chore_log) == 2 and g.stock_amounts()[pid] == 3


def test_done_on_an_unreminded_item_leaves_no_reminder_row(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid = g.create("chores", {"name": "Dust", "period_type": "daily", "period_interval": 1})
    c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-18"}, cookies={"who": str(josh)})
    c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-18"}, cookies={"who": str(josh)})  # stale
    assert len(g.chore_log) == 1 and house.keys() == []


def test_manual_chore_has_no_occurrence_so_app_done_never_double_runs(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    g.update("chores", cid, {"period_type": "manually"})  # Grocy gives it no next date
    for data in ({}, {"due": ""}, {"due": "2026-11-18"}, {}):  # stale page, blank, old date, double tap
        r = c.post(f"/home/chores/{cid}/done", data=data, cookies={"who": str(josh)})
        assert r.status_code == 303 and "Already+done" in r.headers["location"]
    assert g.chore_log == [] and g.stock_amounts()[pid] == 5


def test_unparseable_due_is_stale_not_500(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    for bad in ("abc", "2026-13-45", "\u0662\u0660\u0662\u0666-11-18"):
        r = c.post(f"/home/chores/{cid}/done", data={"due": bad}, cookies={"who": str(josh)})
        assert r.status_code == 303 and "Already+done" in r.headers["location"], bad
        r = c.post(f"/today/chore/{cid}/done", data={"due": bad}, cookies={"who": str(josh)})
        assert r.status_code == 303 and "Already+done" in r.headers["location"], bad
    assert g.chore_log == []
