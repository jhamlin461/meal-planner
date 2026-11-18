from datetime import date, datetime, time, timedelta

from app.config import Settings
from app.house import HouseStore, Reminder
from app.reminders import in_quiet, items, recipients, tick
from tests.fakes import FakeGrocy, FakeHA

JOSH_PHONE, CAT_PHONE = "mobile_app_josh", "mobile_app_cat"


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm)


def world(tmp_path):
    g = FakeGrocy()
    g.today = date(2026, 11, 18)
    house = HouseStore(tmp_path / "house.db")
    josh = house.save_person("Josh", 3, "person.josh", JOSH_PHONE)
    cat = house.save_person("Cat", 4, "person.cat", CAT_PHONE)
    ha = FakeHA()
    ha.states = {"person.josh": "home", "person.cat": "home"}
    settings = Settings("", "", "", "KEY", None, 6, tmp_path, 3, ha_url="http://ha")
    return g, house, ha, settings, josh, cat


def chore(g, house, remind_at="18:00", who=3, **rem):
    cid = g.create("chores", {"name": "Scoop the litter boxes", "period_type": "daily", "period_interval": 1,
                              "assignment_type": "no-assignment", "next_execution_assigned_to_user_id": who})
    house.save_reminder(Reminder(f"chore:{cid}", remind_at=remind_at, **rem))
    return cid


def task(g, house, due="2026-11-18", **rem):
    tid = g.create("tasks", {"name": rem.pop("name", "Call the vet"), "due_date": due, "done": 0,
                             "assigned_to_user_id": None})
    house.save_reminder(Reminder(f"task:{tid}", **rem))
    return tid


def sends(ha, run):
    before = len(ha.sent)
    run()
    return ha.sent[before:]


def test_fires_at_remind_at_not_before(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    ha.states["person.josh"] = "not_home"  # an assigned person is reminded even when away
    cid = chore(g, house)
    [it] = items(g, house, at(18, 17, 59))
    assert (it.key, it.title, it.audience, it.verb, it.repeat_min) == (
        f"chore:{cid}", "Scoop the litter boxes", f"person:{josh}", "Done", 120)
    assert it.due == datetime(2026, 11, 18) and it.fire_at == at(18, 18) and it.icon == "cat"
    tick(g, house, ha, s, at(18, 17, 59))
    assert ha.sent == []
    tick(g, house, ha, s, at(18, 18))
    assert ha.sent == [{"service": JOSH_PHONE, "title": "Scoop the litter boxes", "message": it.context,
                        "tag": f"chore:{cid}",
                        "actions": [(f"HM:done:chore:{cid}:{josh}", "Done"),
                                    (f"HM:snooze:chore:{cid}:{josh}", "Snooze 15 min")]}]
    r = house.reminder(f"chore:{cid}")
    assert r.last_sent == at(18, 18) and r.sent_to == [JOSH_PHONE] and r.sent_for == "2026-11-18T00:00:00"


def test_items_window_and_overdue(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    late = task(g, house, due="2026-11-16", remind_at="18:00")
    task(g, house, due="2026-11-21")  # end of today + 3 days: included
    task(g, house, due="2026-11-22")  # too far out
    task(g, house, due=None)  # no due date: never reminded
    got = {i.key: i for i in items(g, house, at(18, 8))}
    assert sorted(got) == [f"task:{late}", f"task:{late + 1}"]
    assert got[f"task:{late}"].fire_at == datetime(2026, 11, 16)  # overdue: fires now, not at 18:00
    assert got[f"task:{late}"].audience == "home"


def test_repeats_after_interval(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    chore(g, house)
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 18)))) == 1
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 19, 59))) == []
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 20)))) == 1
    # laundry: whoever is home, every 15 minutes, "Moved it"
    tid = task(g, house, name="Move the laundry", audience="home", remind_at="10:00", repeat_min=15, verb="Moved it")
    out = sends(ha, lambda: tick(g, house, ha, s, at(18, 10)))
    assert sorted(m["service"] for m in out) == [CAT_PHONE, JOSH_PHONE]
    assert out[0]["actions"][0][1] == "Moved it" and out[0]["tag"] == f"task:{tid}"
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 14))) == []
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 15)))) == 2
    assert sorted(house.reminder(f"task:{tid}").sent_to) == [CAT_PHONE, JOSH_PHONE]


def test_snooze_waits(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    cid = chore(g, house)
    tick(g, house, ha, s, at(18, 18))
    r = house.reminder(f"chore:{cid}")
    r.snooze_until = at(18, 18, 20)
    house.save_reminder(r)
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 18, 19))) == []
    # the snooze ending reminds again, without waiting out the 120-minute repeat
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 18, 20)))) == 1
    assert house.reminder(f"chore:{cid}").snooze_until is None
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 18, 30))) == []


def test_whoever_home_targets_only_people_home(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    ha.states["person.cat"] = "not_home"
    task(g, house, audience="home", remind_at="10:00")
    out = sends(ha, lambda: tick(g, house, ha, s, at(18, 10)))
    assert [m["service"] for m in out] == [JOSH_PHONE]
    # people with no phone, and people deleted since the audience was chosen, are skipped
    house.save_person("Guest", 5, "person.guest", "")
    ha.states["person.guest"] = "home"
    [it] = items(g, house, at(18, 10))
    assert [p.name for p in recipients(it, house.people(), ha.states)] == ["Josh"]
    it.audience = f"person:{cat}"
    assert [p.name for p in recipients(it, house.people(), ha.states)] == ["Cat"]  # even when away
    house.delete_person(cat)
    assert recipients(it, house.people(), ha.states) == []


def test_someone_arriving_between_repeats_hears_at_once(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    ha.states["person.cat"] = "not_home"
    task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 20))) == []
    ha.states["person.cat"] = "home"
    assert [m["service"] for m in sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 30)))] == [CAT_PHONE]
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 12)))) == 2  # the repeat goes to both, on time


def test_nobody_home_holds_until_someone_arrives(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    ha.states = {"person.josh": "not_home", "person.cat": "not_home"}
    tid = task(g, house, audience="home", remind_at="10:00")
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 10))) == []
    assert house.reminder(f"task:{tid}").last_sent is None
    ha.states["person.cat"] = "home"
    out = sends(ha, lambda: tick(g, house, ha, s, at(18, 11, 30)))
    assert [m["service"] for m in out] == [CAT_PHONE]


def test_quiet_hours_hold_then_send_once(tmp_path):
    quiet = (time(22), time(7))
    assert [in_quiet(at(18, h, m), quiet) for h, m in ((21, 59), (22, 0), (6, 59), (7, 0))] == [False, True, True, False]
    assert not in_quiet(at(18, 12), quiet) and in_quiet(at(19, 0, 30), quiet)
    g, house, ha, s, josh, cat = world(tmp_path)
    task(g, house, due="2026-11-17", audience=f"person:{josh}")  # overdue: fires as soon as it may
    sent_at = []
    now = at(18, 21, 50)
    while now <= at(19, 7, 10):
        sent_at += [now] * len(sends(ha, lambda: tick(g, house, ha, s, now)))
        now += timedelta(minutes=10)
    assert sent_at == [at(18, 21, 50), at(19, 7, 0)]


def test_done_elsewhere_clears_everywhere(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    # a one-off task, completed in Grocy
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    g.complete_task(tid)
    tick(g, house, ha, s, at(18, 10, 1))
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()
    # a recurring chore, done on the other phone: it moves to tomorrow, both phones clear, its settings stay
    ha.cleared.clear()
    cid = chore(g, house, audience="home", repeat_min=30)
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 18)))) == 2
    g.execute_chore(cid, 4)
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 18, 30))) == []
    assert sorted(ha.cleared) == [(CAT_PHONE, f"chore:{cid}"), (JOSH_PHONE, f"chore:{cid}")]
    assert house.reminder(f"chore:{cid}") == Reminder(f"chore:{cid}", "home", "18:00", 30)
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(19, 18)))) == 2  # tomorrow's turn reminds afresh
    # a chore deleted in Grocy
    ha.cleared.clear()
    g.delete("chores", cid)
    tick(g, house, ha, s, at(19, 18, 1))
    assert len(ha.cleared) == 2 and f"chore:{cid}" not in house.keys()


class FlakyHA(FakeHA):
    """Reaches Josh's phone but not Cat's."""

    def notify(self, service, *a):
        if service == CAT_PHONE:
            from app.ha import HAError
            raise HAError("phone gone")
        super().notify(service, *a)


def test_ha_down_sets_flag_and_recovers(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    tid = task(g, house, audience="home", remind_at="10:00")
    ha.down = True
    tick(g, house, ha, s, at(18, 10))
    assert house.flag("ha_error") and ha.sent == []
    ha.down = False
    tick(g, house, ha, s, at(18, 10, 1))
    assert house.flag("ha_error") is None and len(ha.sent) == 2
    # clearing fails while HA is down: retried next tick
    g.complete_task(tid)
    ha.down = True
    tick(g, house, ha, s, at(18, 10, 2))
    assert house.flag("ha_error") and f"task:{tid}" in house.keys()
    ha.down = False
    tick(g, house, ha, s, at(18, 10, 3))
    assert len(ha.cleared) == 2 and f"task:{tid}" not in house.keys() and house.flag("ha_error") is None
    # one phone fails mid-send: the flag is set, the reached phone counts as sent, only the failed one is retried
    flaky = FlakyHA()
    flaky.states = ha.states
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, flaky, s, at(18, 11))
    r = house.reminder(f"task:{tid}")
    assert house.flag("ha_error") and r.sent_to == [JOSH_PHONE] and r.last_sent == at(18, 11)
    assert [m["service"] for m in sends(ha, lambda: tick(g, house, ha, s, at(18, 11, 1)))] == [CAT_PHONE]
    r = house.reminder(f"task:{tid}")
    assert house.flag("ha_error") is None and r.last_sent == at(18, 11) and sorted(r.sent_to) == [CAT_PHONE, JOSH_PHONE]


def test_create_app_starts_loop_only_with_ha_url(tmp_path, monkeypatch):
    import app.web as web
    from app.importer import PendingStore
    from tests.fakes import FakeAI
    started = []
    monkeypatch.setattr(web, "start_loop", lambda fn: started.append(fn()))
    g, house, ha, s, josh, cat = world(tmp_path)
    plain = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    web.create_app(g, FakeAI(), None, plain, PendingStore(tmp_path / "p"), house=house, ha=ha)
    assert started == []
    web.create_app(g, FakeAI(), None, s, PendingStore(tmp_path / "p"), house=house, ha=ha)
    assert started == [(g, house, ha, s)]


class DeadCatHA(FakeHA):
    """Cat's phone was replaced: HA answers 400/404 for her notify service, every time."""

    def __init__(self, clear_status=404):
        super().__init__()
        self.clear_status = clear_status

    def notify(self, service, *a):
        from app.ha import HAError
        if service == CAT_PHONE:
            raise HAError("400 unknown service", status=400)
        super().notify(service, *a)

    def clear(self, service, tag):
        from app.ha import HAError
        if service == CAT_PHONE:
            raise HAError("cat clear failed", status=self.clear_status)
        super().clear(service, tag)


def test_dead_phone_neither_spams_nor_starves(tmp_path):
    g, house, _, s, josh, cat = world(tmp_path)
    ha = DeadCatHA()
    ha.states = {"person.josh": "home", "person.cat": "home"}
    both = task(g, house, audience="home", remind_at="10:00")
    mine = task(g, house, audience=f"person:{josh}", remind_at="10:00")
    for m in range(5):
        tick(g, house, ha, s, at(18, 10, m))
    assert sorted(x["tag"] for x in ha.sent) == [f"task:{both}", f"task:{mine}"]
    assert house.reminder(f"task:{both}").sent_to == [JOSH_PHONE] and house.flag("ha_error")


def test_clear_on_a_gone_phone_forgets_it_and_a_failed_clear_retries(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    g.complete_task(tid)
    gone = DeadCatHA(clear_status=404)
    gone.states = ha.states
    tick(g, house, gone, s, at(18, 10, 1))  # 404: Cat's phone no longer exists, nothing left to clear
    assert gone.cleared == [(JOSH_PHONE, f"task:{tid}")] and f"task:{tid}" not in house.keys()
    # a recurring chore moved on, but HA dropped the connection clearing Cat's phone: kept, retried next tick
    cid = chore(g, house, audience="home")
    tick(g, house, ha, s, at(18, 18))
    g.execute_chore(cid, 3)
    flaky = DeadCatHA(clear_status=None)
    flaky.states = ha.states
    tick(g, house, flaky, s, at(18, 18, 1))
    r = house.reminder(f"chore:{cid}")
    assert r.sent_to == [CAT_PHONE] and r.sent_for == "2026-11-18T00:00:00" and r.last_sent is None
    assert house.flag("ha_error")
    ha.cleared.clear()
    tick(g, house, ha, s, at(18, 18, 2))
    assert ha.cleared == [(CAT_PHONE, f"chore:{cid}")]
    assert house.reminder(f"chore:{cid}") == Reminder(f"chore:{cid}", "home", "18:00") and house.flag("ha_error") is None


def test_ha_flag_clears_during_quiet_hours(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    task(g, house, due="2026-11-17", audience="home")
    ha.down = True
    tick(g, house, ha, s, at(18, 22, 10))
    assert house.flag("ha_error")
    ha.down = False
    tick(g, house, ha, s, at(18, 22, 30))
    assert house.flag("ha_error") is None and ha.sent == []


def test_start_loop_survives_a_failing_tick():
    import threading
    from app.reminders import start_loop
    calls, second = [], threading.Event()

    def state():
        calls.append(1)
        if len(calls) > 1:
            second.set()
            threading.Event().wait()  # park the daemon thread
        raise RuntimeError("Grocy is down")

    start_loop(state, interval=0)
    assert second.wait(5)


def test_home_save_waits_for_the_reminder_lock(tmp_path):
    import threading
    from app import home as hm
    from app.house import LOCK
    g, house, ha, s, josh, cat = world(tmp_path)
    with LOCK:  # as if a tick were running
        t = threading.Thread(target=hm.save, args=(g, house, "chores", "chore", {"name": "Mop"}, {"remind_at": "07:00"}))
        t.start()
        t.join(0.3)
        assert t.is_alive() and house.keys() == []
    t.join(5)
    assert [house.reminder(k).remind_at for k in house.keys()] == ["07:00"]


def test_deleting_on_the_home_page_clears_every_phone(tmp_path):
    import app.web as web
    from fastapi.testclient import TestClient
    from app.importer import PendingStore
    from tests.fakes import FakeAI
    g, house, ha, s, josh, cat = world(tmp_path)
    plain = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    c = TestClient(web.create_app(g, FakeAI(), None, plain, PendingStore(tmp_path / "p"), house=house, ha=ha),
                   follow_redirects=False)
    tid = task(g, house, audience="home", remind_at="10:00")
    tick(g, house, ha, s, at(18, 10))
    assert c.post(f"/home/tasks/{tid}/delete").status_code == 303
    tick(g, house, ha, s, at(18, 10, 1))
    assert sorted(ha.cleared) == [(CAT_PHONE, f"task:{tid}"), (JOSH_PHONE, f"task:{tid}")]
    assert f"task:{tid}" not in house.keys()


def test_laundry_reminder_targets_people_home_every_15_min(tmp_path):
    from app.reminders import washer_finished
    g, house, ha, s, josh, cat = world(tmp_path)
    ha.states["person.cat"] = "not_home"
    assert washer_finished(g, house, at(18, 10, 7)) == "created"
    out = sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 7)))
    assert [(m["service"], m["title"], m["actions"][0][1]) for m in out] == [(JOSH_PHONE, "Move the laundry", "Moved it")]
    assert out[0]["tag"] == house.flag("laundry")
    assert sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 21))) == []
    assert len(sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 22)))) == 1
    ha.states["person.cat"] = "home"
    assert [m["service"] for m in sends(ha, lambda: tick(g, house, ha, s, at(18, 10, 23)))] == [CAT_PHONE]


def test_stale_done_leaves_the_current_occurrence_alone(tmp_path):
    """A Done from an old page (yesterday's occurrence, already done) runs nothing and doesn't clear today's
    notifications or snooze; the tick clears what is really finished."""
    from app.reminders import complete, snooze
    g, house, ha, s, josh, cat = world(tmp_path)
    cid = chore(g, house, audience="home")
    assert complete(g, house, ha, f"chore:{cid}", None, "2026-11-18")
    g.today = date(2026, 11, 19)
    tick(g, house, ha, s, at(19, 18))
    before = house.reminder(f"chore:{cid}")
    assert before.sent_to and before.sent_for == "2026-11-19T00:00:00"
    assert not complete(g, house, ha, f"chore:{cid}", None, "2026-11-18")  # the stale page
    assert ha.cleared == [] and house.reminder(f"chore:{cid}") == before
    snooze(house, ha, f"chore:{cid}", at(19, 18, 5))
    before = house.reminder(f"chore:{cid}")
    assert not complete(g, house, ha, f"chore:{cid}", None, "2026-11-18")
    assert house.reminder(f"chore:{cid}") == before and before.snooze_until == at(19, 18, 20)
    assert len(g.chore_log) == 1
