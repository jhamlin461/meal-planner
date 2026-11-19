from app import ha_setup
from tests.test_ha_live import FakeConn

DONE, STATUS = "event.front_load_washer_notification", "sensor.front_load_washer_current_status"
STATES = [
    {"entity_id": DONE, "state": "unknown", "attributes": {"event_types": ["washing_is_complete", "error"]}},
    {"entity_id": "event.front_load_washer_remote", "state": "unknown", "attributes": {"event_types": ["pressed"]}},
    {"entity_id": "event.dryer_notification", "state": "unknown", "attributes": {"event_types": ["washing_is_complete"]}},
    {"entity_id": STATUS, "state": "power_off", "attributes": {}},
    {"entity_id": "sensor.front_load_washer_remaining_time", "state": "0", "attributes": {}},
    {"entity_id": "person.jhamlin", "state": "home", "attributes": {}},
]
ENV = {"HA_URL": "http://homeassistant.lan", "HA_TOKEN": "tok", "APP_URL": "https://house.tail1.ts.net"}
OLD = {"id": "house_manager", "url_path": "house-manager", "title": "House", "mode": "storage"}


def setup(conn, env=ENV):
    lines, urls = [], []

    def connect(url):
        urls.append(url)
        if isinstance(conn, Exception):
            raise conn
        return conn

    code = ha_setup.main(env, connect=connect, out=lines.append)
    return code, "\n".join(lines), urls


def test_finds_washer_entities():
    assert ha_setup.find_washer(STATES) == ([DONE], [STATUS])
    assert ha_setup.find_washer([]) == ([], [])


def test_creates_the_dashboard_when_missing():
    c = FakeConn(results={"get_states": STATES, "lovelace/dashboards/list": [{"id": "x", "url_path": "energy-x"}]})
    code, out, urls = setup(c)
    assert code == 0 and urls == ["ws://homeassistant.lan/api/websocket"] and c.closed
    assert f"WASHER_DONE_EVENT={DONE}" in out.splitlines() and f"WASHER_STATUS={STATUS}" in out.splitlines()
    assert [m["type"] for m in c.sent[1:]] == ["get_states", "lovelace/dashboards/list", "lovelace/dashboards/create",
                                                "lovelace/config/save"]
    create, save = c.sent[3], c.sent[4]
    assert {k: create[k] for k in ("url_path", "title", "icon", "show_in_sidebar", "require_admin", "mode")} == {
        "url_path": "house-manager", "title": "House", "icon": "mdi:home-heart", "show_in_sidebar": True,
        "require_admin": False, "mode": "storage"}
    # the panel view HA's Webpage dashboard builds, plus camera access for the scanner
    assert save["url_path"] == "house-manager"
    assert save["config"] == {"views": [{"type": "panel", "title": "House", "cards": [
        {"type": "iframe", "url": "https://house.tail1.ts.net", "allow": "camera; fullscreen"}]}]}
    assert "Added" in out and "packages" not in out  # nothing to migrate


def test_reuses_the_interim_dashboard_and_warns_about_the_yaml():
    states = STATES + [{"entity_id": "automation.house_notification_action", "state": "on", "attributes": {}}]
    c = FakeConn(results={"get_states": states, "lovelace/dashboards/list": [OLD]})
    code, out, _ = setup(c)
    assert code == 0
    types = [m["type"] for m in c.sent[1:]]
    assert "lovelace/dashboards/create" not in types and types[-2:] == ["lovelace/dashboards/update",
                                                                        "lovelace/config/save"]
    upd = c.sent[-2]
    assert upd["dashboard_id"] == "house_manager" and "url_path" not in upd and "mode" not in upd
    assert upd["title"] == "House" and upd["show_in_sidebar"] is True
    assert "Updated" in out and "packages/house.yaml" in out and "packages/meal_planner.yaml" in out
    # the dashboard alone, or some other automation.house_*, is no sign of the YAML install
    states = STATES + [{"entity_id": "automation.house_lights", "state": "on", "attributes": {}}]
    code, out, _ = setup(FakeConn(results={"get_states": states, "lovelace/dashboards/list": [OLD]}))
    assert code == 0 and "Updated" in out and "packages" not in out


def test_a_dashboard_failure_still_reports_the_washer_and_the_yaml():
    states = STATES + [{"entity_id": "sensor.meal_planner_expiring", "state": "0", "attributes": {}}]
    c = FakeConn(results={"get_states": states, "lovelace/dashboards/list": []}, fail={"lovelace/dashboards/create"})
    code, out, _ = setup(c)
    assert code == 2 and f"WASHER_DONE_EVENT={DONE}" in out.splitlines()
    assert "House sidebar" in out and "Unauthorized" in out and "packages/house.yaml" in out


def test_no_answer_does_not_hang():
    code, out, _ = setup(FakeConn(silent={"get_states"}))
    assert code == 1 and "no answer" in out


def test_no_app_url_skips_the_sidebar():
    c = FakeConn(results={"get_states": STATES})
    code, out, _ = setup(c, {**ENV, "APP_URL": ""})
    assert code == 0 and [m["type"] for m in c.sent[1:]] == ["get_states"]
    assert "tailscale serve" in out


def test_failures_are_reported_plainly():
    code, out, _ = setup(FakeConn(auth="auth_invalid"))
    assert code == 1 and "refused the token" in out
    code, out, _ = setup(OSError("Connection refused"))
    assert code == 1 and "Connection refused" in out
    code, out, _ = setup(FakeConn(), {"HA_URL": "", "HA_TOKEN": ""})
    assert code == 1 and "HA_URL" in out
