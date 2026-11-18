from app.reminders import washer_finished
from datetime import datetime
from tests.test_reminders import chore, task
from tests.test_today import client, setup


def test_summary_counts_per_person(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    chore(g, house, who=3, remind_at="00:00")
    chore(g, house, who=4, remind_at="00:00")
    task(g, house, name="Call the vet", remind_at="00:00")  # whoever is home: both
    g.volatile_result = {**g.volatile_result, "due_products": [{"product_id": 1, "best_before_date": "2026-11-20"}],
                         "missing_products": [{"id": 1, "name": "Flour"}, {"id": 2, "name": "Salt"}]}
    j = client(tmp_path, g, house, ha).get("/api/summary").json()
    assert set(j["people"]) == {"Josh", "Cat"}
    assert j["people"]["Josh"]["now"] == len(j["people"]["Josh"]["titles"]) == 2
    assert "Call the vet" in j["people"]["Cat"]["titles"] and j["people"]["Cat"]["now"] == 2
    assert j["use_soon"] == 1 and j["running_low"] == 2


def test_summary_reports_laundry_and_ha_state(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    j = c.get("/api/summary").json()
    assert j["laundry_open"] is False and j["ha_ok"] is True
    washer_finished(g, house, datetime.now())
    house.set_flag("ha_error", "Home Assistant unreachable: x")
    ha.down = True
    j = c.get("/api/summary").json()  # a dead HA must not break it
    assert j["laundry_open"] is True and j["ha_ok"] is False


def test_scan_has_open_in_browser_link(tmp_path):
    g, house, ha, josh, cat = setup(tmp_path)
    c = client(tmp_path, g, house, ha)
    assert 'href="/scan" target="_blank"' in c.get("/scan").text
    assert 'href="/scan?from=chores" target="_blank"' in c.get("/scan?from=chores").text
