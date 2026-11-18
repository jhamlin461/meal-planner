from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
from starlette.datastructures import FormData as MultiDict
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.house import HouseStore
from app.importer import PendingStore
from app.reminders import tick
from app.setup_grocy import bootstrap
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy, FakeHA


@pytest.fixture
def hv(tmp_path):
    g = FakeGrocy()
    g.today = date(2026, 11, 18)
    bootstrap(g)
    house = HouseStore(tmp_path / "house.db")
    a = g.create_user("josh", "Josh")
    b = g.create_user("cat", "Cat")
    pa = house.save_person("Josh", a, "", "")
    pb = house.save_person("Cat", b, "", "")
    app = create_app(g, FakeAI(), httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     Settings("", "", "", "KEY", None, 6, tmp_path, 3), PendingStore(tmp_path / "pending"),
                     house=house, ha=FakeHA())
    return g, house, TestClient(app, follow_redirects=False), (pa, a), (pb, b)


def supply(g, name="Trash bags"):
    group = next(x["id"] for x in g.objects("product_groups") if x["name"] == "Household supplies")
    return g.create("products", {"name": name, "product_group_id": group})


def test_create_weekly_chore_with_supply(hv):
    g, house, c, *_ = hv
    pid = supply(g)
    r = c.post("/home/chores/new", data={"name": "Take out the trash", "schedule": "weekly", "days": "tuesday",
                                         "who": "turns", "remind_at": "19:00", "repeat_min": "",
                                         "product_id": str(pid), "product_amount": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/home")
    (ch,) = g.objects("chores")
    assert (ch["period_type"], ch["period_config"]) == ("weekly", "tuesday")
    assert ch["consume_product_on_chore_execution"] == 1 and ch["product_amount"] == 1 and ch["product_id"] == pid
    rem = house.reminder(f"chore:{ch['id']}")
    assert rem.remind_at == "19:00" and rem.repeat_min is None


def test_take_turns_sets_who_least_did_first(hv):
    g, house, c, (_, a), (_, b) = hv
    c.post("/home/chores/new", data={"name": "Dishes", "schedule": "daily", "who": "turns", "remind_at": "09:00"})
    (ch,) = g.objects("chores")
    assert ch["assignment_type"] == "who-least-did-first"
    assert sorted(ch["assignment_config"].split(",")) == sorted([str(a), str(b)])
    assert g.chores_status()[0]["next_execution_assigned_to_user_id"] in (a, b)  # recalculated after the save


def test_one_person_chore_and_every_n_days(hv):
    g, house, c, (pa, a), _ = hv
    c.post("/home/chores/new", data={"name": "Mop", "schedule": "every", "every": "3", "who": str(pa),
                                     "remind_at": "08:30", "repeat_min": "45"})
    (ch,) = g.objects("chores")
    # one person = a rotation of one (Grocy recalculates the assignee on every Done; no-assignment would lose it)
    assert (ch["period_type"], ch["period_interval"], ch["assignment_type"],
            ch["assignment_config"]) == ("dynamic-regular", 3, "who-least-did-first", str(a))
    assert ch["period_days"] == 3
    assert house.reminder(f"chore:{ch['id']}").repeat_min == 45
    assert g.chores_status()[0]["next_execution_assigned_to_user_id"] == a  # assigned before its first Done
    c.post(f"/home/chores/{ch['id']}/done", data={"due": "2026-11-18"}, cookies={"who": "kitchen"})
    assert g.chores_status()[0]["next_execution_assigned_to_user_id"] == a  # and still after it
    assert f'<option value="{pa}" selected>' in c.get(f"/home/chores/{ch['id']}").text  # reads back as one person


def test_bad_chore_form_is_refused_with_message(hv):
    g, house, c, *_ = hv
    r = c.post("/home/chores/new", data={"name": " ", "schedule": "daily", "who": "turns"})
    assert r.status_code == 400 and 'role="alert"' in r.text and not g.objects("chores")


def test_edit_chore(hv):
    g, house, c, *_ = hv
    c.post("/home/chores/new", data={"name": "Dishes", "schedule": "daily", "who": "turns", "remind_at": "09:00"})
    (ch,) = g.objects("chores")
    assert "Dishes" in c.get(f"/home/chores/{ch['id']}").text
    c.post(f"/home/chores/{ch['id']}", data={"name": "Wash up", "schedule": "monthly", "monthly": "5", "who": "turns",
                                             "remind_at": "10:00"})
    (ch,) = g.objects("chores")
    assert (ch["name"], ch["period_type"], ch["period_days"]) == ("Wash up", "monthly", 5)
    assert house.reminder(f"chore:{ch['id']}").remind_at == "10:00"


def test_task_whoever_home_stored_as_audience_home(hv):
    g, house, c, (pa, a), _ = hv
    c.post("/home/tasks/new", data={"title": "Fix the gate", "note": "hinge", "due": "2026-11-20", "who": "home",
                                    "remind_at": "17:00"})
    (t,) = g.objects("tasks")
    assert t["name"] == "Fix the gate" and t["due_date"] == "2026-11-20"
    assert house.reminder(f"task:{t['id']}").audience == "home"
    c.post("/home/tasks/new", data={"title": "Call vet", "who": str(pa), "remind_at": "09:00"})
    t2 = next(t for t in g.objects("tasks") if t["name"] == "Call vet")
    assert house.reminder(f"task:{t2['id']}").audience == f"person:{pa}" and t2["assigned_to_user_id"] == a
    c.post(f"/home/tasks/{t2['id']}/done", data={"due": ""})  # no due date: always its one occurrence
    assert [t["name"] for t in g.open_tasks()] == ["Fix the gate"]


def test_upkeep_charge(hv):
    g, house, c, *_ = hv
    c.post("/home/upkeep/new", data={"name": "Smoke alarm", "every": "90", "remind_at": "10:00"})
    (b,) = g.objects("batteries")
    assert b["charge_interval_days"] == 90 and house.reminder(f"battery:{b['id']}").remind_at == "10:00"
    assert "Smoke alarm" in c.get("/home").text
    c.post(f"/home/upkeep/{b['id']}/done", data={"due": "2027-02-16"})
    assert len(g.battery_log) == 1
    c.post(f"/home/upkeep/{b['id']}/delete")
    assert not g.objects("batteries") and gone_after_tick(g, house)


def test_done_credits_device_person(hv):
    g, house, c, _, (pb, b) = hv
    cid = g.create("chores", {"name": "Dishes", "period_type": "daily", "period_interval": 1})
    c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-18"}, cookies={"who": str(pb)})
    assert g.chore_log[-1]["done_by"] == b and not g.chore_log[-1]["skipped"]


def test_kitchen_done_credits_assigned_person(hv):
    g, house, c, (pa, a), _ = hv
    cid = g.create("chores", {"name": "Mop", "period_type": "daily", "assignment_type": "who-least-did-first",
                              "assignment_config": str(a)})
    g.calculate_next_assignments()
    c.post(f"/home/chores/{cid}/done", data={"due": "2026-11-18"}, cookies={"who": "kitchen"})
    assert g.chore_log[-1]["done_by"] == a
    c.post(f"/home/chores/{cid}/skip", data={"due": "2026-11-19"}, cookies={"who": "kitchen"})  # credits the same way
    assert g.chore_log[-1]["done_by"] == a and g.chore_log[-1]["skipped"]


def test_skip_moves_date_without_consuming(hv):
    g, house, c, (pa, a), _ = hv
    pid = supply(g)
    g.add_stock(pid, 3, None, None)
    cid = g.create("chores", {"name": "Trash", "period_type": "daily", "period_interval": 1,
                              "consume_product_on_chore_execution": 1, "product_id": pid, "product_amount": 1})
    assert f'action="/home/chores/{cid}/skip"><input type="hidden" name="due" value="2026-11-18">' in c.get("/home").text
    c.post(f"/home/chores/{cid}/skip", data={"due": "2026-11-18"}, cookies={"who": str(pa)})
    assert g.stock_amounts()[pid] == 3 and g.chore_log[-1]["skipped"]
    assert g.chores_status()[0]["next_estimated_execution_time"].startswith("2026-11-19")


def gone_after_tick(g, house) -> bool:
    """Deleting leaves the reminder for the loop, which clears the phones it reached and then drops it."""
    tick(g, house, FakeHA(), Settings("", "", "", "KEY", None, 6, Path("."), 3), datetime(2026, 11, 18, 12))
    return house.keys() == []


def test_delete_chore_drops_reminder(hv):
    g, house, c, *_ = hv
    c.post("/home/chores/new", data={"name": "Dishes", "schedule": "daily", "who": "turns", "remind_at": "09:00"})
    (ch,) = g.objects("chores")
    c.post(f"/home/chores/{ch['id']}/delete")
    assert not g.objects("chores") and gone_after_tick(g, house)


def test_home_shows_empty_state_then_lists(hv):
    g, house, c, *_ = hv
    assert "Add starter chores" in c.get("/home").text
    c.post("/home/starters")
    html = c.get("/home").text
    assert "Scoop the litter boxes" in html and "Add starter chores" not in html and "style=" not in html


def test_starters_added_once(hv):
    g, house, c, *_ = hv
    c.post("/home/starters")
    assert len(g.objects("chores")) == 3
    c.post("/home/starters")
    assert len(g.objects("chores")) == 3
    scoop = next(x for x in g.objects("chores") if x["name"].startswith("Scoop"))
    assert house.reminder(f"chore:{scoop['id']}").remind_at == "18:00"


def test_starter_trash_links_garbage_bags_when_present(hv):
    g, house, c, *_ = hv
    pid = supply(g, "Garbage bags 30 ct")
    c.post("/home/starters")
    trash = next(x for x in g.objects("chores") if x["name"] == "Take out the trash")
    assert trash["product_id"] == pid and trash["consume_product_on_chore_execution"] == 1
    assert house.reminder(f"chore:{trash['id']}").remind_at == "19:00"
    litter = next(x for x in g.objects("chores") if x["name"] == "Change the litter")
    assert litter["period_type"] == "dynamic-regular" and litter["consume_product_on_chore_execution"] == 0


def test_forms_render_and_task_edit_round_trips(hv):
    g, house, c, (pa, a), _ = hv
    for u in ("/home/chores/new", "/home/tasks/new", "/home/upkeep/new"):
        html = c.get(u).text
        assert "style=" not in html and 'class="field"' in html
    c.post("/home/tasks/new", data={"title": "Call vet", "who": str(pa), "remind_at": "09:00"})
    (t,) = g.objects("tasks")
    assert f'value="{pa}" selected' in c.get(f"/home/tasks/{t['id']}").text
    assert c.get("/home/chores/999").status_code == 404


def test_take_turns_without_people_is_refused(tmp_path):
    from app import home
    with pytest.raises(ValueError, match="Add people"):
        home.chore_fields(MultiDict(name="x", schedule="daily", who="turns"), [])


def test_take_turns_with_no_people_via_form(hv):
    g, house, c, (pa, _), (pb, _) = hv
    house.delete_person(pa)
    house.delete_person(pb)
    r = c.post("/home/chores/new", data={"name": "Dishes", "schedule": "daily", "who": "turns"})
    assert r.status_code == 400 and "Add people on the People page first" in r.text and not g.objects("chores")


def test_every_n_days_round_trip_and_stale_fields_cleared(hv):
    g, house, c, *_ = hv
    c.post("/home/chores/new", data={"name": "Litter", "schedule": "every", "every": "14", "who": "turns"})
    (ch,) = g.objects("chores")
    assert (ch["period_type"], ch["period_days"], ch["period_interval"]) == ("dynamic-regular", 14, 14)
    html = c.get(f"/home/chores/{ch['id']}").text
    assert 'value="14"' in html and 'value="every" selected' in html
    c.post(f"/home/chores/{ch['id']}", data={"name": "Litter", "schedule": "weekly", "days": "friday", "who": "turns"})
    c.post(f"/home/chores/{ch['id']}", data={"name": "Litter", "schedule": "daily", "who": "turns"})
    (ch,) = g.objects("chores")
    assert ch["period_type"] == "daily" and not ch["period_config"] and not ch["period_days"]


def test_starter_litter_edit_keeps_14_days(hv):
    g, house, c, *_ = hv
    c.post("/home/starters")
    litter = next(x for x in g.objects("chores") if x["name"] == "Change the litter")
    assert 'value="14"' in c.get(f"/home/chores/{litter['id']}").text


def test_grocy_error_on_save_rerenders_form(hv):
    from app.grocy import GrocyError
    g, house, c, *_ = hv
    g.create = lambda *a, **k: (_ for _ in ()).throw(GrocyError("duplicate name"))
    r = c.post("/home/chores/new", data={"name": "Dishes", "schedule": "daily", "who": "turns"})
    assert r.status_code == 400 and 'role="alert"' in r.text and 'value="Dishes"' in r.text


def test_delete_missing_is_404(hv):
    g, house, c, *_ = hv
    for k in ("chores", "tasks", "upkeep"):
        assert c.post(f"/home/{k}/999/delete").status_code == 404


def test_home_sentences_and_no_middle_dot(hv):
    g, house, c, (pa, a), _ = hv
    g.create("chores", {"name": "Mop", "period_type": "daily", "next_execution_assigned_to_user_id": a})
    g.create("chores", {"name": "Dishes", "period_type": "daily", "assignment_type": "who-least-did-first",
                        "assignment_config": ""})
    html = c.get("/home").text
    assert chr(183) not in html and "Due today. Josh&#39;s turn." in html and "Due today. Takes turns." in html


def test_no_done_button_without_due_date(hv):
    """A manually scheduled chore or an upkeep item with no cycle has no due date to tell one Done from the next."""
    g, house, c, *_ = hv
    cid = g.create("chores", {"name": "Descale the kettle", "period_type": "manually"})
    bid = g.create("batteries", {"name": "Spare remote"})
    tid = g.create("tasks", {"name": "Someday task", "done": 0})
    html = c.get("/home").text
    assert f"/home/chores/{cid}/done" not in html and f"/home/chores/{cid}/skip" not in html
    assert f"/home/upkeep/{bid}/done" not in html and html.count("Mark this one in Grocy.") == 2
    assert f"/home/tasks/{tid}/done" in html  # a task with no date still has its one occurrence


def test_double_skip_skips_once(hv):
    g, house, c, (pa, a), _ = hv
    cid = g.create("chores", {"name": "Dishes", "period_type": "daily", "period_interval": 1})
    r = c.post(f"/home/chores/{cid}/skip", data={"due": "2026-11-18"}, cookies={"who": str(pa)})
    assert "Skipped." in unquote_plus(r.headers["location"])
    r = c.post(f"/home/chores/{cid}/skip", data={"due": "2026-11-18"}, cookies={"who": str(pa)})
    assert "Already done." in unquote_plus(r.headers["location"]) and len(g.chore_log) == 1
    c.post(f"/home/chores/{cid}/skip", cookies={"who": str(pa)})  # no due (an old cached page): never skips
    assert len(g.chore_log) == 1


def test_skip_after_notification_done_is_a_no_op(hv):
    from app.reminders import complete
    g, house, c, (pa, a), _ = hv
    cid = g.create("chores", {"name": "Dishes", "period_type": "daily", "period_interval": 1})
    assert complete(g, house, FakeHA(), f"chore:{cid}", None, "2026-11-18")  # the phone's Done ran first
    r = c.post(f"/home/chores/{cid}/skip", data={"due": "2026-11-18"}, cookies={"who": str(pa)})
    assert "Already done." in unquote_plus(r.headers["location"])
    assert [x["skipped"] for x in g.chore_log] == [False]
