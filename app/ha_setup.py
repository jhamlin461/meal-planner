"""python -m app.ha_setup — run by setup.sh inside the app container (which also proves the container reaches HA).
Finds the washer's entities (printed as WASHER_DONE_EVENT=… / WASHER_STATUS=… lines for setup.sh) and adds the
"House" sidebar dashboard showing the app (APP_URL), updating the one an earlier install made."""
import os
import re
import sys

from app.ha_live import CONNECT, Session, ws_url

URL_PATH = "house-manager"
DASHBOARD = {"title": "House", "icon": "mdi:home-heart", "show_in_sidebar": True, "require_admin": False}
# what the interim YAML packages (house.yaml, meal_planner.yaml) put in HA
OLD_YAML = {"automation.house_notification_action", "automation.house_washer_finished",
            "automation.house_washer_started", "sensor.meal_planner_expiring"}
DASHBOARD_FAILED = 2  # exit code: washer found (lines printed), but the sidebar dashboard couldn't be made


def find_washer(states: list[dict]) -> tuple[list[str], list[str]]:
    """(washer event entities that report washing_is_complete, washer current-status sensors)"""
    done = [s["entity_id"] for s in states if s["entity_id"].startswith("event.") and "washer" in s["entity_id"]
            and "washing_is_complete" in ((s.get("attributes") or {}).get("event_types") or [])]
    status = [s["entity_id"] for s in states if re.fullmatch(r"sensor\.\w*washer\w*current_status", s["entity_id"])]
    return done, status


def view(app_url: str) -> dict:
    return {"views": [{"type": "panel", "title": "House",
                       "cards": [{"type": "iframe", "url": app_url, "allow": "camera; fullscreen"}]}]}


def dashboard(s: Session, app_url: str) -> str:
    """Create or update the sidebar dashboard showing the app full screen; "created" or "updated".
    HA's Webpage dashboard type builds this same panel view but passes only the url, so its frame gets the default
    allow="fullscreen" and the barcode scanner can't open the camera; this view adds it."""
    old = next((d for d in s.call(type="lovelace/dashboards/list") or [] if d.get("url_path") == URL_PATH), None)
    if old:
        s.call(type="lovelace/dashboards/update", dashboard_id=old["id"], **DASHBOARD)
    else:
        s.call(type="lovelace/dashboards/create", url_path=URL_PATH, mode="storage", **DASHBOARD)
    s.call(type="lovelace/config/save", url_path=URL_PATH, config=view(app_url))
    return "updated" if old else "created"


def main(env=os.environ, connect=CONNECT, out=print) -> int:
    url, token, app_url = env.get("HA_URL", ""), env.get("HA_TOKEN", ""), env.get("APP_URL", "").rstrip("/")
    if not url or not token:
        out("HA_URL and HA_TOKEN must be set (deploy/.env).")
        return 1
    code = 0
    try:
        conn = connect(ws_url(url))
        try:
            s = Session(conn, token)
            states = s.call(type="get_states") or []
            done, status = find_washer(states)
            for line in [f"WASHER_DONE_EVENT={e}" for e in done] + [f"WASHER_STATUS={e}" for e in status]:
                out(line)
            if not done or not status:
                out("Didn't find the washer in Home Assistant (an event.*washer* entity with washing_is_complete and "
                    "a sensor.*washer*current_status): laundry reminders need both.")
            if not app_url:
                out("No app URL (tailscale serve isn't set up yet), so the House sidebar was skipped; re-run setup.sh "
                    "after `tailscale serve --bg 8000`.")
            else:
                try:
                    made = dashboard(s, app_url)
                    out(f'{"Added" if made == "created" else "Updated"} the "House" dashboard in Home Assistant\'s '
                        f"sidebar (shows {app_url}).")
                except Exception as e:
                    out(f"Couldn't add the House sidebar dashboard: {e}")
                    code = DASHBOARD_FAILED
        finally:
            conn.close()
    except Exception as e:
        out(f"Couldn't set up Home Assistant from the app container: {e}")
        return 1
    if OLD_YAML & {st.get("entity_id") for st in states}:
        out("IMPORTANT: the app now talks to Home Assistant itself. Delete packages/house.yaml and "
            "packages/meal_planner.yaml from Home Assistant's config folder and restart Home Assistant, or phone "
            "taps are handled twice and the old REST sensors clash with the app's sensor.house_* ones.")
    return code


if __name__ == "__main__":
    sys.exit(main())
