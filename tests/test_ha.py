import json

import httpx
import pytest

from app.ha import HA, HAError


def make(handler):
    return HA("http://ha", "t", transport=httpx.MockTransport(handler))


def test_notify_posts_tag_and_actions():
    seen = {}

    def h(req):
        seen["path"], seen["auth"], seen["body"] = req.url.path, req.headers["Authorization"], json.loads(req.content)
        return httpx.Response(200, json=[])

    make(h).notify("mobile_app_pixel_10_pro", "Laundry", "Moved it?", "chore:3", [("HM:done:chore:3:1", "Done")])
    assert seen["path"] == "/api/services/notify/mobile_app_pixel_10_pro"
    assert seen["auth"] == "Bearer t"
    assert seen["body"]["message"] == "Moved it?" and seen["body"]["title"] == "Laundry"
    d = seen["body"]["data"]
    assert d["tag"] == "chore:3"
    assert d["actions"] == [{"action": "HM:done:chore:3:1", "title": "Done"}]
    assert d["channel"] == "House reminders" and d["ttl"] == 0 and d["priority"] == "high"


def test_clear_sends_clear_notification():
    seen = {}

    def h(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json=[])

    make(h).clear("mobile_app_cat_s_pixel_10", "chore:3")
    assert seen["path"] == "/api/services/notify/mobile_app_cat_s_pixel_10"
    assert seen["body"] == {"message": "clear_notification", "data": {"tag": "chore:3"}}


STATES = [
    {"entity_id": "person.jhamlin", "state": "home", "attributes": {"friendly_name": "Jhamlin"}},
    {"entity_id": "person.catherine", "state": "not_home", "attributes": {"friendly_name": "Catherine"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
]


def test_home_states_only_persons():
    ha = make(lambda r: httpx.Response(200, json=STATES))
    assert ha.home_states() == {"person.jhamlin": "home", "person.catherine": "not_home"}
    assert ha.persons() == {"person.jhamlin": "Jhamlin", "person.catherine": "Catherine"}


def test_notify_services_mobile_only():
    svc = [
        {"domain": "notify", "services": {"mobile_app_pixel_10_pro": {}, "mobile_app_cat_s_pixel_10": {}, "notify": {}}},
        {"domain": "light", "services": {"mobile_app_fake": {}}},
    ]
    ha = make(lambda r: httpx.Response(200, json=svc))
    assert sorted(ha.notify_services()) == ["mobile_app_cat_s_pixel_10", "mobile_app_pixel_10_pro"]


def test_http_error_raises_haerror():
    with pytest.raises(HAError) as e:
        make(lambda r: httpx.Response(500, text="boom")).home_states()
    assert e.value.status == 500
    with pytest.raises(HAError) as e:
        make(lambda r: httpx.Response(400, text="unknown service")).clear("mobile_app_gone", "chore:1")
    assert e.value.status == 400

    def boom(req):
        raise httpx.ConnectError("down")

    with pytest.raises(HAError) as e:
        make(boom).notify("mobile_app_x", "t", "m", "k", [])
    assert e.value.status is None


def test_non_json_reply_raises_haerror():
    with pytest.raises(HAError):
        make(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})).home_states()


def test_person_trackers():
    states = [
        {"entity_id": "person.jhamlin", "state": "home",
         "attributes": {"friendly_name": "Jhamlin", "device_trackers": ["device_tracker.pixel_10_pro"]}},
        {"entity_id": "person.guest", "state": "home", "attributes": {}},
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    ]
    ha = make(lambda r: httpx.Response(200, json=states))
    assert ha.person_trackers() == {"person.jhamlin": ["device_tracker.pixel_10_pro"], "person.guest": []}
