from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.house import Reminder
from app.importer import PendingStore
from app.reminders import tick, washer_finished
from app.web import create_app
from tests.fakes import FakeAI
from tests.test_reminders import CAT_PHONE, JOSH_PHONE, at, chore, task, world

SECRET = {"X-House-Secret": "s3cret"}
NO_QUIET = (time(0), time(0))


def client(tmp_path, g, house, ha, secret="s3cret"):
    s = Settings("", "", "", "KEY", None, 6, tmp_path, 3, hook_secret=secret, quiet_hours=NO_QUIET)
    return TestClient(create_app(g, FakeAI(), None, s, PendingStore(tmp_path / "p"), house=house, ha=ha),
                      follow_redirects=False)


def test_hook_requires_secret(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    for path in ("/api/hook/action", "/api/events/washer-finished", "/api/events/washer-started"):
        assert c.post(path, json={"action": "HM:done:task:1:1"}).status_code == 403
        assert c.post(path, json={"action": "HM:done:task:1:1"}, headers={"X-House-Secret": "nope"}).status_code == 403
    assert g.objects("tasks") == []
    # no HOOK_SECRET set: hooks are off, even an empty header is refused
    off = client(tmp_path, g, house, ha, secret="")
    assert off.post("/api/events/washer-finished", headers={"X-House-Secret": ""}).status_code == 403
    # a wrong secret is refused before the body is looked at
    assert c.post("/api/hook/action", json={"bad": 1}, headers={"X-House-Secret": "nope"}).status_code == 403


def test_hook_rejects_bad_action_ids(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    post = lambda a: c.post("/api/hook/action", json={"action": a}, headers=SECRET)  # noqa: E731
    r = post(f"HM:done:task:{tid}:999")
    assert r.status_code == 404 and r.json() == {"status": "error", "message": "unknown person"}
    for bad in ("rm -rf", f"HM:done:task:x:{josh}", f"HM:done:robot:{tid}:{josh}", f"HM:done:task:١:{josh}",
                f"HM:done:task:{tid}:١", f"HM:done:task:{'9' * 5000}:{josh}", f"HM:nap:task:{tid}:{josh}"):
        assert post(bad).status_code == 400, bad
    assert g.open_tasks() and house.reminder(f"task:{tid}").sent_for  # nothing was done


def test_action_done_completes_and_clears(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    r = c.post("/api/hook/action", json={"action": f"HM:done:task:{tid}:{cat}"}, headers=SECRET)
    assert r.json() == {"status": "done"} and g.open_tasks() == []
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()
    # a recurring chore: credited to whoever tapped, phones cleared, sent state reset, settings kept
    ha.cleared.clear()
    cid = chore(g, house, audience="home", repeat_min=30, verb="Scooped")
    tick(g, house, ha, s, at(18, 18))
    c.post("/api/hook/action", json={"action": f"HM:done:chore:{cid}:{cat}"}, headers=SECRET)
    assert g.chore_log[-1]["done_by"] == 4
    assert sorted(ha.cleared) == [(CAT_PHONE, f"chore:{cid}"), (JOSH_PHONE, f"chore:{cid}")]
    rem = house.reminder(f"chore:{cid}")
    assert (rem.audience, rem.remind_at, rem.repeat_min, rem.verb) == ("home", "18:00", 30, "Scooped")
    assert (rem.sent_to, rem.last_sent, rem.snooze_until, rem.sent_for) == ([], None, None, None)


def trash(g, house, bags=5):
    pid = g.create("products", {"name": "Garbage bags"})
    g.add_stock(pid, bags, None, None)
    cid = g.create("chores", {"name": "Take out the trash", "period_type": "daily", "period_interval": 1,
                              "assignment_type": "no-assignment", "consume_product_on_chore_execution": 1,
                              "product_id": pid, "product_amount": 1})
    house.save_reminder(Reminder(f"chore:{cid}", "home", "19:00"))
    return cid, pid


def test_double_done_executes_once(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    tick(g, house, ha, s, at(18, 19))
    act = {"action": f"HM:done:chore:{cid}:{josh}"}
    assert c.post("/api/hook/action", json=act, headers=SECRET).json() == {"status": "done"}
    assert c.post("/api/hook/action", json=act, headers=SECRET).json() == {"status": "already-done"}
    # the other phone, a second later
    c.post("/api/hook/action", json={"action": f"HM:done:chore:{cid}:{cat}"}, headers=SECRET)
    assert len(g.chore_log) == 1 and g.stock_amounts()[pid] == 4
    # done in the app first, then a stale notification is tapped: no second run
    tick(g, house, ha, s, at(19, 19))
    assert c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-19"}, cookies={"who": str(cat)}).status_code == 303
    assert c.post("/api/hook/action", json=act, headers=SECRET).json() == {"status": "already-done"}
    assert len(g.chore_log) == 2 and g.stock_amounts()[pid] == 3


def test_snooze_action(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    before = datetime.now()
    assert c.post("/api/hook/action", json={"action": f"HM:snooze:task:{tid}:{josh}"},
                  headers=SECRET).json() == {"status": "snoozed"}
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
    c = client(tmp_path, g, house, ha)
    hh = g.create("task_categories", {"name": "Household"})
    assert c.post("/api/events/washer-finished", headers=SECRET).json() == {"status": "created"}
    assert c.post("/api/events/washer-finished", headers=SECRET).json() == {"status": "already-open"}
    (t,) = g.open_tasks()
    assert (t["name"], t["category_id"], t["assigned_to_user_id"]) == ("Move the laundry", hh, None)
    assert t["due_date"] == date.today().isoformat() and house.flag("laundry") == f"task:{t['id']}"
    r = house.reminder(f"task:{t['id']}")
    assert (r.audience, r.repeat_min, r.verb) == ("home", 15, "Moved it")
    # moved by hand (Moved it): the next load's finish makes a new task
    g.complete_task(t["id"])
    assert c.post("/api/events/washer-finished", headers=SECRET).json() == {"status": "created"}
    assert len(g.open_tasks()) == 1


def test_washer_started_clears_open_laundry(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    assert c.post("/api/events/washer-started", headers=SECRET).json() == {"status": "none-open"}
    washer_finished(g, house, at(18, 10, 7))
    tick(g, house, ha, s, at(18, 10, 7))
    tid = int(house.flag("laundry").split(":")[1])
    assert c.post("/api/events/washer-started", headers=SECRET).json() == {"status": "cleared"}
    assert g.open_tasks() == [] and house.flag("laundry") is None
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()
    assert c.post("/api/events/washer-started", headers=SECRET).json() == {"status": "none-open"}


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


def test_ha_package_has_secret_and_automations():
    text = (Path(__file__).parent.parent / "deploy/ha/house.yaml").read_text()
    for s in ("!secret house_secret", "House: notification action", "House: washer finished", "House: washer started",
              "event.front_load_washer_notification", "washing_is_complete", "sensor.front_load_washer_current_status",
              "rest_command", "/api/hook/action", "/api/events/washer-finished", "/api/events/washer-started"):
        assert s in text, s


def test_done_while_ha_down_runs_once_and_clears_later(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    cid, pid = trash(g, house)
    tick(g, house, ha, s, at(18, 19))
    ha.down = True
    act = {"action": f"HM:done:chore:{cid}:{josh}"}
    assert c.post("/api/hook/action", json=act, headers=SECRET).json() == {"status": "done"}
    assert c.post("/api/hook/action", json=act, headers=SECRET).json() == {"status": "already-done"}
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
    c.post("/api/hook/action", json={"action": f"HM:done:chore:{cid}:{josh}"}, headers=SECRET)
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
