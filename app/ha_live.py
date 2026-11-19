"""Home Assistant, live: phone taps and the washer arrive over HA's WebSocket; the app pushes its sensors and the
morning "use soon" note through HA's REST API. Nothing to install in HA."""
import functools
import json
import logging
import re
import threading
import time as clock
import unicodedata
from datetime import datetime, time

from websockets.sync.client import connect as ws_connect

from app import reminders as rm
from app.ha import HAError
from app.today import expiring, summary

log = logging.getLogger(__name__)
NOT_REAL = ("unavailable", "unknown")  # HA restarts and reloads rewrite entities from these: not a real change
USE_SOON_AT = time(8)
# get_states on a big HA is several MB; websockets' default cap is 1 MB
CONNECT = functools.partial(ws_connect, max_size=2 ** 24, open_timeout=10)


def ws_url(ha_url: str) -> str:
    return re.sub(r"^http", "ws", ha_url.rstrip("/")) + "/api/websocket"


class Session:
    """One authenticated HA WebSocket: commands get increasing ids; call() waits for a command's result."""

    def __init__(self, conn, token: str):
        self.conn, self.n = conn, 0
        if (m := self.recv()).get("type") != "auth_required":
            raise HAError(f"not a Home Assistant WebSocket (got {m.get('type')!r})")
        conn.send(json.dumps({"type": "auth", "access_token": token}))
        if (m := self.recv()).get("type") != "auth_ok":
            raise HAError(f"Home Assistant refused the token: {m.get('message') or m.get('type')}", 401)

    def recv(self) -> dict:
        return json.loads(self.conn.recv())

    def call(self, **msg):
        """Send a command and return its result; other messages arriving meanwhile are dropped."""
        self.n += 1
        self.conn.send(json.dumps({"id": self.n, **msg}))
        while (m := self.recv()).get("id") != self.n or m.get("type") != "result":
            pass
        if not m.get("success"):
            raise HAError(f"{msg['type']}: {(m.get('error') or {}).get('message') or m}")
        return m.get("result")


def _states(event: dict) -> tuple[dict, dict]:
    """(from_state, to_state) of a subscribe_trigger state event; {} for a missing one."""
    t = (event.get("variables") or {}).get("trigger") or {}
    return t.get("from_state") or {}, t.get("to_state") or {}


def washer_done(event: dict) -> bool:
    old, new = _states(event)
    return (bool(old) and old.get("state") not in NOT_REAL
            and (new.get("attributes") or {}).get("event_type") == "washing_is_complete")


def washer_started(event: dict) -> bool:
    old, new = _states(event)
    return (bool(old) and old.get("state") not in NOT_REAL and new.get("state") in ("initial", "detecting")
            and old.get("state") != new.get("state"))


def serve(s: Session, grocy, house, ha, settings, now=datetime.now) -> None:
    """Subscribe, then handle events until the connection drops (recv raises)."""
    def tapped(event):
        a = (event.get("data") or {}).get("action")
        if isinstance(a, str) and a.startswith("HM:"):
            log.info("phone action %s: %s", a, rm.action(grocy, house, ha, a, now()))

    def done(event):
        if washer_done(event):
            log.info("washer finished: %s", rm.washer_finished(grocy, house, now()))

    def started(event):
        if washer_started(event):
            log.info("washer started: %s", rm.washer_started(grocy, house, ha))

    handlers = {}
    s.call(type="subscribe_events", event_type="mobile_app_notification_action")
    handlers[s.n] = tapped
    for entity, fn in ((settings.washer_done_event, done), (settings.washer_status, started)):
        if entity:
            s.call(type="subscribe_trigger", trigger={"platform": "state", "entity_id": entity})
            handlers[s.n] = fn
    while True:
        m = s.recv()
        fn = handlers.get(m.get("id")) if m.get("type") == "event" else None
        if fn is None:
            continue
        try:
            fn(m.get("event") or {})
        except Exception:
            log.exception("Home Assistant event failed: %s", m)
        rm.WAKE.set()


def _down(house, msg: str | None) -> None:
    house.set_flag("ha_live", msg)
    house.set_flag("ha_error", msg)


def loop(grocy, house, ha, settings, connect=CONNECT, sleep=clock.sleep) -> None:
    """Stay connected: reconnect with backoff (5 s doubling to 60 s, back to 5 s after a good connection); while
    disconnected the ha_error flag says so (Today's warning)."""
    delay = 5
    while True:
        connected = False
        try:
            conn = connect(ws_url(settings.ha_url))
            try:
                s = Session(conn, settings.ha_token)
                connected = True
                _down(house, None)
                serve(s, grocy, house, ha, settings)
            finally:
                conn.close()
        except Exception as e:
            log.warning("Home Assistant live connection: %s", e)
            _down(house, f"Home Assistant live connection lost: {e}"[:200])
        delay = 5 if connected else delay
        sleep(delay)
        delay = min(delay * 2, 60)


def start(grocy, house, ha, settings) -> threading.Thread:
    def run():
        try:
            loop(grocy, house, ha, settings)
        except BaseException:  # never expected: loop catches everything; logged so it isn't silent
            log.exception("Home Assistant live connection stopped")
    t = threading.Thread(target=run, name="ha-live", daemon=True)
    t.start()
    return t


# ---- pushed to HA: sensors every minute (and after any write), and the morning "use soon" note ------------------------
def slug(name: str) -> str:
    """HA-style object id: "Zoë O'Neil" -> "zoe_o_neil"."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_.lower()).strip("_") or "person"


def sensors(summ: dict) -> dict[str, tuple[object, dict]]:
    """entity id -> (state, attributes)"""
    out = {f"sensor.house_{slug(name)}_needs_you_now":
           (v["now"], {"titles": v["titles"], "friendly_name": f"House: {name} needs you now",
                       "icon": "mdi:account-alert"})
           for name, v in summ["people"].items()}
    out["sensor.house_use_soon"] = (summ["use_soon"], {"friendly_name": "House: use soon", "icon": "mdi:food-apple"})
    out["sensor.house_running_low"] = (summ["running_low"],
                                       {"friendly_name": "House: running low", "icon": "mdi:cart-arrow-down"})
    out["binary_sensor.house_laundry_open"] = (
        "on" if summ["laundry_open"] else "off", {"friendly_name": "House: laundry open", "icon": "mdi:washing-machine"})
    return out


def use_soon_note(grocy, house, ha, settings, now: datetime) -> None:
    """From 08:00 (once quiet hours are over), once a day: what's expiring, to every phone. Tried again next tick
    until every phone took it (the same tag replaces it on a phone that already has it)."""
    day = now.date().isoformat()
    if now.time() < USE_SOON_AT or rm.in_quiet(now, settings.quiet_hours) or house.flag("use_soon_sent") == day:
        return
    e = expiring(grocy, settings.expiry_warn_days)
    ok = True
    for p in house.people() if e["count"] else []:
        if p.notify_service:
            try:
                ha.notify(p.notify_service, "Food to use soon", e["message"], "house:use-soon", [])
            except HAError as err:
                log.warning("Home Assistant: %s", err)
                ok = False
    if ok:
        house.set_flag("use_soon_sent", day)


def publish(grocy, house, ha, settings, now: datetime) -> None:
    """Run by the reminder loop after each tick."""
    try:
        for entity, (state, attrs) in sensors(summary(grocy, house, ha, now, settings.expiry_warn_days)).items():
            ha.set_state(entity, state, attrs)
    except HAError as e:  # the tick flags HA being down; one failure is enough this minute
        log.warning("Home Assistant: %s", e)
    use_soon_note(grocy, house, ha, settings, now)
