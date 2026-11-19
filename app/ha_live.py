"""Home Assistant, live: phone taps and the washer arrive over HA's WebSocket; the app pushes its sensors and the
morning "use soon" note through HA's REST API. Nothing to install in HA."""
import functools
import json
import logging
import re
import threading
import time as clock
import unicodedata
from collections import Counter
from datetime import datetime, time

from websockets.sync.client import connect as ws_connect

from app import reminders as rm
from app.ha import HAError
from app.today import expiring, summary

log = logging.getLogger(__name__)
NOT_REAL = ("unavailable", "unknown")  # HA restarts and reloads rewrite entities from these: not a real change
USE_SOON = (time(8), time(12))  # the morning note goes out in this window (after quiet hours)
CALL_TIMEOUT = 15  # seconds HA gets to answer a command
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

    def recv(self, timeout: float | None = None) -> dict:
        return json.loads(self.conn.recv() if timeout is None else self.conn.recv(timeout=timeout))

    def call(self, **msg):
        """Send a command and return its result (HAError when HA refuses or doesn't answer in CALL_TIMEOUT);
        other messages arriving meanwhile are dropped."""
        self.n += 1
        self.conn.send(json.dumps({"id": self.n, **msg}))
        deadline = clock.monotonic() + CALL_TIMEOUT
        try:
            while (m := self.recv(max(0.0, deadline - clock.monotonic()))).get("id") != self.n \
                    or m.get("type") != "result":
                pass
        except TimeoutError as e:
            raise HAError(f"{msg['type']}: no answer from Home Assistant in {CALL_TIMEOUT} s") from e
        if not m.get("success"):
            raise HAError(f"{msg['type']}: {(m.get('error') or {}).get('message') or m}")
        return m.get("result")


def _states(event: dict) -> tuple[dict, dict]:
    """(from_state, to_state) of a subscribe_trigger state event; {} for a missing (null) one."""
    t = (event.get("variables") or {}).get("trigger") or {}
    if not t:
        log.debug("unexpected trigger event: %s", event)
    return t.get("from_state") or {}, t.get("to_state") or {}


def washer_done(event: dict) -> bool:
    old, new = _states(event)
    return (bool(old) and old.get("state") not in NOT_REAL
            and (new.get("attributes") or {}).get("event_type") == "washing_is_complete")


def washer_started(event: dict) -> bool:
    old, new = _states(event)
    return (bool(old) and old.get("state") not in NOT_REAL and new.get("state") in ("initial", "detecting")
            and old.get("state") != new.get("state"))


def subscribe(s: Session, grocy, house, ha, settings, now=datetime.now) -> dict:
    """Subscribe to phone taps (essential: an HAError here means not connected) and, each in its own try, the
    washer. Returns subscription id -> handler; a handler returns True when it acted."""
    def tapped(event):
        a = (event.get("data") or {}).get("action")
        if not (isinstance(a, str) and a.startswith("HM:")):
            return False
        log.info("phone action %s: %s", a, rm.action(grocy, house, ha, a, now()))
        return True

    def done(event):
        if washer_done(event):
            log.info("washer finished: %s", rm.washer_finished(grocy, house, now()))
            return True
        return False

    def started(event):
        if washer_started(event):
            log.info("washer started: %s", rm.washer_started(grocy, house, ha))
            return True
        return False

    s.call(type="subscribe_events", event_type="mobile_app_notification_action")
    handlers = {s.n: tapped}
    for entity, fn in ((settings.washer_done_event, done), (settings.washer_status, started)):
        if entity:
            try:
                s.call(type="subscribe_trigger", trigger={"platform": "state", "entity_id": entity})
                handlers[s.n] = fn
            except HAError as e:  # phone taps still work; only laundry reminders are off
                log.warning("Home Assistant refused to watch the washer (%s), so no laundry reminders: %s", entity, e)
    return handlers


def serve(s: Session, handlers: dict) -> None:
    """Handle events until the connection drops (recv raises)."""
    while True:
        m = s.recv()
        fn = handlers.get(m.get("id")) if m.get("type") == "event" else None
        if fn is None:
            log.debug("Home Assistant message ignored: %s", m)
            continue
        try:
            if fn(m.get("event") or {}):
                rm.WAKE.set()  # push the sensors now
        except Exception:
            log.exception("Home Assistant event failed: %s", m)


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
                handlers = subscribe(s, grocy, house, ha, settings)
                connected = True
                _down(house, None)
                serve(s, handlers)
            finally:
                conn.close()
        except Exception as e:
            log.warning("Home Assistant live connection: %s", e)
            try:
                _down(house, f"Home Assistant live connection lost: {e}"[:200])
            except Exception:
                log.exception("couldn't flag the Home Assistant outage")
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
    slugs = Counter(slug(name) for name in summ["people"])
    ids = {name: slug(name) + (f"_{v['id']}" if slugs[slug(name)] > 1 else "") for name, v in summ["people"].items()}
    out = {f"sensor.house_{ids[name]}_needs_you_now":
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
    """Between 08:00 and 12:00 (outside quiet hours), once a day: what's expiring, to every phone. A phone HA
    can't reach is tried again next tick; one that took it, or that HA no longer knows (4xx), isn't tried again."""
    day, (start, end) = now.date().isoformat(), USE_SOON
    if not start <= now.time() < end or rm.in_quiet(now, settings.quiet_hours):
        return
    st = json.loads(house.flag("use_soon") or "{}")
    done = set(st.get("done") or []) if st.get("day") == day else set()
    phones = {p.notify_service for p in house.people() if p.notify_service}
    if phones <= done:
        return
    e = expiring(grocy, settings.expiry_warn_days)
    for service in sorted(phones - done) if e["count"] else []:
        try:
            ha.notify(service, "Food to use soon", e["message"], "house:use-soon", [])
        except HAError as err:
            log.warning("Home Assistant: %s", err)
            if not (err.status and 400 <= err.status < 500):
                continue
        done.add(service)
    house.set_flag("use_soon", json.dumps({"day": day, "done": sorted(done if e["count"] else phones)}))


def publish(grocy, house, ha, settings, now: datetime) -> None:
    """Run by the reminder loop after each tick."""
    try:
        for entity, (state, attrs) in sensors(summary(grocy, house, ha, now, settings.expiry_warn_days)).items():
            ha.set_state(entity, state, attrs)
    except HAError as e:  # the tick flags HA being down; one failure is enough this minute
        log.warning("Home Assistant: %s", e)
    use_soon_note(grocy, house, ha, settings, now)
