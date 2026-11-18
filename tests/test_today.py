import re
from datetime import date, timedelta
from html import unescape

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.ha import HA
from app.importer import PendingStore
from app.reminders import snooze, washer_finished
from app.setup_grocy import bootstrap, ids
from app.today import today_view
from app.web import create_app
from tests.fakes import FakeAI
from tests.test_reminders import at, chore, task, world

TODAY = date.today()  # the page uses the real clock; view-level tests use at() on 2026-11-18


def setup(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    bootstrap(g)
    return g, house, ha, house.person(josh), house.person(cat)


def client(tmp_path, g, house, ha):
    s = Settings("", "", "", "KEY", None, 6, tmp_path, 3)
    return TestClient(create_app(g, FakeAI(), None, s, PendingStore(tmp_path / "p"), house=house, ha=ha),
                      follow_redirects=False)


def page(c, who) -> str:
    r = c.get("/", cookies={"who": str(who)})
    assert r.status_code == 200
    return unescape(r.text)


def keys(view) -> list[str]:
    return [i.key for i in view["now_items"]]


def test_today_redirects_to_who_without_cookie(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    for cookies in ({}, {"who": "999"}, {"who": "nobody"}):
        r = c.get("/", cookies=cookies)
        assert r.status_code == 303 and r.headers["location"] == "/who", cookies


def test_today_lists_my_due_items_only(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    mine = chore(g, house, who=3)  # Josh's turn, remind at 18:00
    theirs = chore(g, house, who=4)
    t = task(g, house, name="Call the vet", audience=f"person:{cat.id}", remind_at="09:00")
    assert keys(today_view(g, house, josh, at(18, 18), ha=ha)) == [f"chore:{mine}"]
    assert sorted(keys(today_view(g, house, cat, at(18, 18), ha=ha))) == [f"chore:{theirs}", f"task:{t}"]
    # before its remind-at time it's coming up, not now
    v = today_view(g, house, josh, at(18, 17), ha=ha)
    assert v["now_items"] == [] and v["coming"][0] == {"title": "Scoop the litter boxes", "when": "Today at 6 PM",
                                                     "soon": True, "icon": "cat"}
    # snoozed: out of "now", still on the screen
    snooze(house, ha, f"chore:{mine}", at(18, 18))
    v = today_view(g, house, josh, at(18, 18, 5), ha=ha)
    assert v["now_items"] == [] and v["coming"][0]["when"] == "Snoozed until 6:15 PM"
    assert keys(today_view(g, house, josh, at(18, 18, 15), ha=ha)) == [f"chore:{mine}"]


def test_today_home_items_only_when_home(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    t = task(g, house, name="Move the laundry", audience="home", remind_at="10:00", verb="Moved it")
    ha.states["person.josh"] = "not_home"
    assert keys(today_view(g, house, josh, at(18, 11), ha=ha)) == []
    assert keys(today_view(g, house, cat, at(18, 11), ha=ha)) == [f"task:{t}"]
    assert today_view(g, house, cat, at(18, 11), ha=ha)["note"] == "Cat, you're home"
    # out: still on the screen, as waiting at home, with nothing to tap
    assert today_view(g, house, josh, at(18, 11), ha=ha)["coming"] == [
        {"title": "Move the laundry", "when": "Waiting at home", "soon": True, "icon": "washing-machine"}]
    # HA unreachable: nobody's whereabouts are known, so everyone sees it and the warning says why
    ha.down = True
    v = today_view(g, house, josh, at(18, 11), ha=ha)
    assert keys(v) == [f"task:{t}"] and v["ha_error"]
    assert keys(today_view(g, house, josh, at(18, 11), ha=None)) == [f"task:{t}"]  # HA not set up


def test_kitchen_device_sees_everyone(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    ha.states = {"person.josh": "not_home", "person.cat": "not_home"}
    yday = (TODAY - timedelta(days=1)).isoformat()
    task(g, house, due=yday, name="Fix the gate", audience=f"person:{josh.id}")
    task(g, house, due=yday, name="Call the vet", audience=f"person:{cat.id}")
    task(g, house, due=yday, name="Move the laundry", audience="home", verb="Moved it")
    html = page(client(tmp_path, g, house, ha), "kitchen")
    assert "3 things need you" in html and "For everyone" in html
    for name in ("Fix the gate", "Call the vet", "Move the laundry", ">Moved it<"):
        assert name in html, name
    assert "Call the vet" not in page(client(tmp_path, g, house, ha), josh.id)


def test_today_headline_counts(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    yday = (TODAY - timedelta(days=1)).isoformat()
    a = task(g, house, due=yday, name="Fix the gate", audience=f"person:{josh.id}")
    task(g, house, due=yday, name="Book the dentist", audience=f"person:{josh.id}")
    html = page(c, josh.id)
    assert '<h1 class="headline">2 things need you<small>Josh, you\'re home</small></h1>' in html
    assert html.count('class="card now"') == 2 and "Overdue since yesterday." in html
    assert "Snooze 15 min" in html and chr(183) not in html
    g.complete_task(a)
    assert "1 thing needs you" in page(c, josh.id)


def test_today_empty_state_names_next_item(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    assert "Nothing needs you<small>Nothing else is due in the next few days</small>" in page(c, josh.id)
    task(g, house, due=(TODAY + timedelta(days=1)).isoformat(), name="Take out the trash",
         audience=f"person:{josh.id}", remind_at="19:00")
    html = page(c, josh.id)
    assert "Nothing needs you<small>Next up: Take out the trash, tomorrow at 7 PM</small>" in html
    assert 'class="card now"' not in html and "Tomorrow at 7 PM" in html


def test_today_shows_tonight_dinner(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    assert "No dinner planned yet." in page(c, josh.id)
    i = ids(g)
    rid = g.create("recipes", {"name": "Chicken tacos", "base_servings": 4})
    g.create("meal_plan", {"day": TODAY.isoformat(), "type": "recipe", "recipe_id": rid, "recipe_servings": "4.0",
                           "section_id": i.section["Dinner"], "note": "", "done": 0})
    tomorrow = TODAY + timedelta(days=1)
    g.create("meal_plan", {"day": tomorrow.isoformat(), "type": "note", "note": "Leftovers: Chicken tacos",
                           "section_id": i.section["Lunch"], "done": 0})
    html = page(c, josh.id)
    assert "<h2>Tonight</h2>" in html and "Chicken tacos for 4" in html
    assert f"Leftovers become {tomorrow:%A}'s lunch" in html and "No dinner planned" not in html


def test_today_coming_up_food_and_supplies(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    spinach = g.create("products", {"name": "Spinach"})
    g.create("products", {"name": "Garbage bags", "min_stock_amount": 1})  # none in stock: Grocy says missing
    g.volatile_result["due_products"] = [{"product_id": spinach, "best_before_date": "2026-11-20"}]
    task(g, house, due="2026-11-19", name="Recycling out", remind_at="19:00", audience=f"person:{josh.id}")
    v = today_view(g, house, josh, at(18, 9), ha=ha, warn_days=3)
    assert v["coming"] == [
        {"title": "Recycling out", "when": "Tomorrow at 7 PM", "soon": False, "icon": "calendar-check"},
        {"title": "Spinach", "when": "Use by Friday", "soon": True, "icon": "fridge-outline"},
        {"title": "Garbage bags", "when": "Running low", "soon": False, "icon": "basket-outline"}]
    # a week or more out: the date, not a weekday that could be this week's
    g.volatile_result["due_products"] = [{"product_id": spinach, "best_before_date": "2026-11-27"}]
    assert today_view(g, house, josh, at(18, 9), ha=ha, warn_days=10)["coming"][1]["when"] == "Use by November 27"
    html = page(client(tmp_path, g, house, ha), josh.id)
    assert 'class="tick"' not in html and '<use href="/static/icons.svg#basket-outline">' in html


def test_today_shows_ha_warning(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    house.set_flag("ha_error", "Home Assistant unreachable: down")
    html = page(c, josh.id)
    assert "Phone reminders aren't reaching Home Assistant" in html and 'role="alert"' in html
    assert "Check that Home Assistant is running" in html
    house.set_flag("ha_error", None)  # the loop clears it once HA answers
    assert "Phone reminders aren't reaching Home Assistant" not in page(c, josh.id)


def test_today_done_button_completes(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    g.today = TODAY
    cid = chore(g, house, who=3, remind_at="00:00")  # due today, fired at midnight
    html = page(c, josh.id)
    form = re.search(r'action="(/today/chore/\d+/done)"><input type="hidden" name="due" value="([^"]*)"', html)
    assert form and form.group(1) == f"/today/chore/{cid}/done" and form.group(2) == TODAY.isoformat()
    assert f'action="/today/chore/{cid}/snooze"' in html
    r = c.post(form.group(1), data={"due": form.group(2)}, cookies={"who": str(josh.id)})
    assert r.status_code == 303 and r.headers["location"] == "/?msg=Done."
    assert g.chore_log[-1]["done_by"] == 3
    after = unescape(c.get(r.headers["location"], cookies={"who": str(josh.id)}).text)
    assert 'role="status">Done.' in after and "Nothing needs you" in after


def contexts(view) -> list[str]:
    return [i.context for i in view["now_items"]]


def test_today_card_context_says_who_and_what(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    chore(g, house, who=3, remind_at="09:00")  # Josh's turn
    task(g, house, name="Call the vet", audience=f"person:{cat.id}", due="2026-11-16")
    washer_finished(g, house, at(18, 16, 20))
    now = at(18, 16, 30)
    assert contexts(today_view(g, house, "kitchen", now, ha=ha)) == [
        "For Cat, overdue since Monday.", "Josh's turn today.", "The washer finished at 4:20 PM."]
    assert contexts(today_view(g, house, josh, now, ha=ha)) == ["Your turn today.", "The washer finished at 4:20 PM."]
    assert contexts(today_view(g, house, cat, now, ha=ha)) == ["Overdue since Monday.", "The washer finished at 4:20 PM."]
    task(g, house, name="Fix the gate", audience="home", remind_at="09:00")
    assert "Whoever is home, due today." in contexts(today_view(g, house, "kitchen", now, ha=ha))
    # the notification text is untouched
    from app.reminders import items
    assert {i.context for i in items(g, house, now)} <= {"Due today", "Overdue since Mon Nov 16"}


def test_today_never_waits_on_a_hung_home_assistant(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    t = task(g, house, name="Move the laundry", audience="home", remind_at="10:00")
    asked = []

    def hang(request):
        asked.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("no answer", request=request)

    v = today_view(g, house, josh, at(18, 11), ha=HA("http://ha", "t", transport=httpx.MockTransport(hang)))
    assert asked == [2] and keys(v) == [f"task:{t}"] and v["ha_error"]


def test_today_skips_chores_without_a_due_date(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    g.create("chores", {"name": "Descale the kettle", "period_type": "manually", "next_execution_assigned_to_user_id": 3})
    v = today_view(g, house, josh, at(18, 11), ha=ha)
    assert v["now_items"] == [] and v["coming"] == []


def test_today_blank_dinner_note_is_no_dinner(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    g.create("meal_plan", {"day": "2026-11-18", "type": "note", "note": " ", "section_id": ids(g).section["Dinner"],
                           "done": 0})
    assert today_view(g, house, josh, at(18, 11), ha=ha)["tonight"] is None
