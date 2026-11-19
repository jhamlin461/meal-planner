import json
from dataclasses import replace
from datetime import time

import pytest

from app import ha_live
from app import reminders as rm
from app.reminders import tick, washer_finished
from app.setup_grocy import bootstrap
from tests.test_reminders import CAT_PHONE, JOSH_PHONE, at, chore, world

DONE, STATUS = "event.front_load_washer_notification", "sensor.front_load_washer_current_status"
NO_QUIET = (time(0), time(0))


class FakeConn:
    """A scripted HA WebSocket. Answers auth and every command; then plays `events` — (event type or entity id,
    payload) — on the matching subscription's id, and drops (raises) when they run out."""

    def __init__(self, events=(), auth="auth_ok", results=None, on_drop=None):
        self.events, self.auth, self.results, self.on_drop = list(events), auth, results or {}, on_drop
        self.sent, self.inbox, self.subs, self.closed = [], [{"type": "auth_required"}], {}, False

    def send(self, text):
        m = json.loads(text)
        self.sent.append(m)
        if m["type"] == "auth":
            self.inbox.append({"type": self.auth, "message": "Invalid access token"})
            return
        if m["type"] == "subscribe_events":
            self.subs[m["event_type"]] = m["id"]
        if m["type"] == "subscribe_trigger":
            self.subs[m["trigger"]["entity_id"]] = m["id"]
        self.inbox.append({"id": m["id"], "type": "result", "success": True, "result": self.results.get(m["type"])})

    def recv(self):
        if self.inbox:
            return json.dumps(self.inbox.pop(0))
        if self.events:
            key, payload = self.events.pop(0)
            return json.dumps({"id": self.subs[key], "type": "event", "event": payload})
        if self.on_drop:
            self.on_drop()
        raise ConnectionError("closed")

    def close(self):
        self.closed = True


class Stop(Exception):
    pass


def run(g, house, ha, s, conns, sleeps=None, max_sleeps=1):
    """ha_live.loop until it has slept max_sleeps times; conns: FakeConns (or exceptions) for each connect."""
    conns, sleeps = list(conns), [] if sleeps is None else sleeps
    urls = []

    def connect(url):
        urls.append(url)
        c = conns.pop(0) if conns else OSError("refused")
        if isinstance(c, Exception):
            raise c
        return c

    def sleep(n):
        sleeps.append(n)
        if len(sleeps) >= max_sleeps:
            raise Stop

    with pytest.raises(Stop):
        ha_live.loop(g, house, ha, s, connect=connect, sleep=sleep)
    return urls


def tap(action):
    return "mobile_app_notification_action", {"event_type": "mobile_app_notification_action", "data": {"action": action}}


def change(entity, old, new, **attrs):
    st = lambda v, a: None if v is None else {"entity_id": entity, "state": v, "attributes": a}  # noqa: E731
    return entity, {"variables": {"trigger": {"platform": "state", "entity_id": entity,
                                              "from_state": st(old, {}), "to_state": st(new, attrs)}}}


def test_connects_and_subscribes(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    s = replace(s, ha_url="https://ha.example.ts.net/", ha_token="tok", washer_done_event=DONE, washer_status=STATUS)
    c = FakeConn()
    assert run(g, house, ha, s, [c]) == ["wss://ha.example.ts.net/api/websocket"]
    assert c.sent[0] == {"type": "auth", "access_token": "tok"} and c.closed
    assert [(m["id"], m["type"]) for m in c.sent[1:]] == [(1, "subscribe_events"), (2, "subscribe_trigger"),
                                                          (3, "subscribe_trigger")]
    assert c.sent[1]["event_type"] == "mobile_app_notification_action"
    assert [m["trigger"] for m in c.sent[2:]] == [{"platform": "state", "entity_id": DONE},
                                                 {"platform": "state", "entity_id": STATUS}]
    assert ha_live.ws_url("http://homeassistant.lan") == "ws://homeassistant.lan/api/websocket"
    # no washer configured: no washer subscriptions
    c = FakeConn()
    run(g, house, ha, replace(s, washer_done_event="", washer_status=""), [c])
    assert [m["type"] for m in c.sent[1:]] == ["subscribe_events"]


def test_phone_taps_complete_a_chore_once(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    cid = chore(g, house, audience="home")
    tick(g, house, ha, s, at(18, 18))
    rm.WAKE.clear()
    c = FakeConn([tap(f"HM:done:chore:{cid}:{josh}"), tap(f"HM:done:chore:{cid}:{josh}"),
                  tap(f"HM:done:chore:{cid}:{cat}"), tap("SOMETHING_ELSE"), tap(f"HM:done:chore:{cid}:999"),
                  ("mobile_app_notification_action", {"data": {}})])
    run(g, house, ha, s, [c])
    assert len(g.chore_log) == 1 and g.chore_log[-1]["done_by"] == 3
    assert sorted(ha.cleared) == [(CAT_PHONE, f"chore:{cid}"), (JOSH_PHONE, f"chore:{cid}")]
    assert rm.WAKE.is_set()  # the sensors are pushed right away


def test_a_failing_handler_keeps_the_connection(tmp_path, monkeypatch):
    g, house, ha, s, josh, cat = world(tmp_path)
    calls = []

    def boom(*a):
        calls.append(a[3])
        raise RuntimeError("Grocy is down")

    monkeypatch.setattr(rm, "action", boom)
    c = FakeConn([tap("HM:done:chore:1:1"), tap("HM:done:chore:2:1")])
    run(g, house, ha, s, [c])
    assert calls == ["HM:done:chore:1:1", "HM:done:chore:2:1"]


def test_washer_finished_and_started(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    s = replace(s, washer_done_event=DONE, washer_status=STATUS)
    g.create("task_categories", {"name": "Household"})
    stamp = "2026-11-18T18:02:11.000+00:00"
    c = FakeConn([
        change(DONE, "unavailable", stamp, event_type="washing_is_complete"),  # HA restart rewrites the entity
        change(DONE, "unknown", stamp, event_type="washing_is_complete"),
        change(DONE, None, stamp, event_type="washing_is_complete"),
        change(DONE, "2026-11-17T09:00:00.000+00:00", stamp, event_type="error"),
    ])
    run(g, house, ha, s, [c])
    assert g.open_tasks() == []
    c = FakeConn([change(DONE, "2026-11-17T09:00:00.000+00:00", stamp, event_type="washing_is_complete"),
                  change(DONE, stamp, "2026-11-18T18:05:00.000+00:00", event_type="washing_is_complete")])
    run(g, house, ha, s, [c])
    (t,) = g.open_tasks()
    assert t["name"] == "Move the laundry" and house.flag("laundry") == f"task:{t['id']}"
    c = FakeConn([change(STATUS, "unavailable", "initial"), change(STATUS, "unknown", "detecting"),
                  change(STATUS, "end", "power_off"), change(STATUS, "initial", "initial")])
    run(g, house, ha, s, [c])
    assert g.open_tasks() and house.flag("laundry")
    c = FakeConn([change(STATUS, "power_off", "initial")])
    run(g, house, ha, s, [c])
    assert g.open_tasks() == [] and house.flag("laundry") is None


def test_reconnects_with_backoff_and_flags_the_outage(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    seen = []
    ok = FakeConn(on_drop=lambda: seen.append(house.flag("ha_error")))
    sleeps = []
    run(g, house, ha, s, [OSError("refused")] * 6 + [ok, FakeConn(auth="auth_invalid")], sleeps, max_sleeps=9)
    assert sleeps == [5, 10, 20, 40, 60, 60, 5, 10, 20]  # connected once: back to 5 s
    assert seen == [None]  # cleared while connected
    assert house.flag("ha_error") and house.flag("ha_live")
    # a tick that reaches HA's REST API keeps the warning while the live connection is down
    tick(g, house, ha, s, at(18, 9))
    assert house.flag("ha_error")


def test_sensors_pushed(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    bootstrap(g)
    house.save_person("Mary Ann", 5, "person.mary", "")
    chore(g, house, who=3, remind_at="00:00")
    g.volatile_result = {**g.volatile_result, "due_products": [{"product_id": 1, "best_before_date": "2026-11-20"}],
                         "missing_products": [{"id": 1, "name": "Flour"}, {"id": 2, "name": "Salt"}]}
    washer_finished(g, house, at(18, 10))
    ha_live.publish(g, house, ha, replace(s, quiet_hours=NO_QUIET), at(18, 11))
    st = ha.pushed
    assert set(st) == {"sensor.house_josh_needs_you_now", "sensor.house_cat_needs_you_now",
                       "sensor.house_mary_ann_needs_you_now", "sensor.house_use_soon", "sensor.house_running_low",
                       "binary_sensor.house_laundry_open"}
    josh_s = st["sensor.house_josh_needs_you_now"]
    assert josh_s["state"] == "2" and "Scoop the litter boxes" in josh_s["attributes"]["titles"]
    assert josh_s["attributes"]["friendly_name"] == "House: Josh needs you now" and josh_s["attributes"]["icon"]
    assert st["sensor.house_use_soon"]["state"] == "1" and st["sensor.house_running_low"]["state"] == "2"
    assert st["binary_sensor.house_laundry_open"]["state"] == "on"
    assert ha_live.slug("Zoë O'Neil") == "zoe_o_neil"
    # HA down: no crash, nothing pushed
    ha.pushed.clear()
    ha.down = True
    ha_live.publish(g, house, ha, s, at(18, 11))
    assert ha.pushed == {}


def test_use_soon_note_once_a_day_from_8(tmp_path):
    g, house, ha, s, josh, cat = world(tmp_path)
    bootstrap(g)
    s = replace(s, quiet_hours=(time(22), time(7)))
    house.save_person("Guest", 5, "person.guest", "")  # no phone: not notified
    pid = g.create("products", {"name": "Milk"})
    g.volatile_result = {**g.volatile_result, "due_products": [{"product_id": pid, "best_before_date": "2026-11-20"}]}

    def note(now):
        ha_live.publish(g, house, ha, s, now)
        return [m for m in ha.sent if m["tag"] == "house:use-soon"]

    assert note(at(18, 7, 59)) == []
    sent = note(at(18, 8))
    assert sorted(m["service"] for m in sent) == [CAT_PHONE, JOSH_PHONE]
    assert "Milk" in sent[0]["message"] and sent[0]["actions"] == []
    assert len(note(at(18, 8, 1))) == 2 and len(note(at(18, 20))) == 2  # once a day
    # quiet until 09:00 on the 19th: held until then
    s = replace(s, quiet_hours=(time(22), time(9)))
    assert len(note(at(19, 8, 30))) == 2
    assert len(note(at(19, 9))) == 4
    # HA down at 8: tried again on the next tick
    ha.down = True
    ha_live.publish(g, house, ha, s, at(20, 9))
    ha.down = False
    assert len(note(at(20, 9, 1))) == 6
    # nothing expiring: no note
    g.volatile_result = {**g.volatile_result, "due_products": []}
    assert len(note(at(21, 9))) == 6


def test_a_write_wakes_the_loop(tmp_path):
    from tests.test_today import client
    g, house, ha, s, josh, cat = world(tmp_path)
    c = client(tmp_path, g, house, ha)
    rm.WAKE.clear()
    c.get("/")
    assert not rm.WAKE.is_set()
    c.post("/today/task/1/snooze")
    assert rm.WAKE.is_set()
