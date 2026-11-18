# Home Assistant set up by the app itself, and a layout that grows with the screen

Owner requests (2026-11-18): "The setup file has an API key to communicate with HA. Can't it do it during setup?"
and "I want the UX to be dynamic based on the size of the screen accessing it. I will probably do a lot of recipe
imports from a desktop."

Global rules: the house-manager plan's Global Constraints; load `frontend-design:frontend-design` for UI work; TDD;
existing tests are the behavioural contract except where this plan removes a feature on purpose.

## Task C: The app configures and talks to Home Assistant directly (no YAML)

HA's REST API can't write YAML, but everything the package did can be done with the app's own long-lived token:

- **HA connection** (`app/ha_live.py`): one background thread keeps a Home Assistant WebSocket open
  (`<HA_URL>/api/websocket`, auth with `HA_TOKEN`; `websockets` is already installed via `uvicorn[standard]` —
  use its sync client), reconnecting with backoff (5 s → 60 s). While disconnected, the `ha_error` flag says so
  (Today's warning). It subscribes to:
  - `mobile_app_notification_action` events → actions starting `HM:` go to `reminders.action(...)` exactly as the
    hook did (same validation, same occurrence guard, same LOCK).
  - the washer: `subscribe_trigger` on the configured finished event entity (state change whose new
    `attributes.event_type == "washing_is_complete"`, ignoring changes from `unavailable`/`unknown`) and on the
    configured status sensor (to `initial` or `detecting`, not from `unavailable`/`unknown`) → the same
    washer-finished / washer-started logic the hooks used.
- **Sensors pushed by the app**: each reminder tick (and after any Done/Snooze/Bought) the app `POST`s
  `/api/states/...` for `sensor.house_<person>_needs_you_now` (state = count, attributes: titles, friendly_name,
  icon), `sensor.house_use_soon`, `sensor.house_running_low`, `binary_sensor.house_laundry_open`. (Entities set this
  way can't be edited in HA's UI and reappear on the next tick after an HA restart — note it in the README.)
- **Daily "use soon" notification** replaces the old YAML automation: at 08:00 local (outside quiet hours), if
  anything is expiring, each person with a phone gets one notification listing it (tag `house:use-soon`).
- **HA setup command** `python -m app.ha_setup` (run by `setup.sh` inside the app container after the HA token
  is checked — this also proves the container itself can reach HA):
  1. Finds the washer entities: `event.*` entities whose id contains "washer" and has `event_types` including
     `washing_is_complete`, and `sensor.*washer*current_status`; prints what it found; `setup.sh` stores them as
     `WASHER_DONE_EVENT` / `WASHER_STATUS` in `.env` (asks if none or several are found; blank = no laundry
     reminders).
  2. Creates (or updates) the sidebar dashboard over the WebSocket: `lovelace/dashboards/list`, then
     `lovelace/dashboards/create` {url_path: "house-manager", title: "House", icon: "mdi:home-heart",
     show_in_sidebar: true, require_admin: false, mode: "storage"} if missing, then `lovelace/config/save` with one
     panel view holding a webpage (`iframe`) card for the app's HTTPS URL (from `tailscale serve status`, passed in
     by setup.sh as `APP_URL`).
  3. Prints the result in plain words.
- **Migration from the interim YAML install** (already live in the owner's HA: `packages/house.yaml`,
  `packages/meal_planner.yaml`, a `packages:` line, and a "House" Webpage dashboard with url_path `house-manager`):
  `ha_setup` must reuse that dashboard (update, don't duplicate) and print a clear note that the two package files
  must be deleted from HA's `packages/` folder before the new version runs, otherwise notification taps would be
  handled twice (harmless thanks to the occurrence guard, but noisy) and the YAML REST sensors would clash with the
  pushed `sensor.house_*` entities. README gets the same note. App person names come from People (the owner's are
  "Joshua" and "Catherine"); pushed sensor ids use the slugified name (`sensor.house_joshua_needs_you_now`).
- **Removed**: the `/api/hook/action` and `/api/events/washer-*` endpoints, `HOOK_SECRET`, `deploy/ha/house.yaml`,
  `deploy/ha/configuration.yaml`, `deploy/ha/automation.yaml`, and their README steps; `/api/summary` and
  `/api/expiring` stay (read-only).
- **README**: Home Assistant section becomes: install the HA app on both phones (notifications on, Tailscale, External
  URL), create a long-lived token for the app (profile → Security), run `setup.sh`; first-run checks.
- **Tests**: a fake WebSocket server (or an injected fake connection object) driving: notification action → chore
  done once; duplicate action → once; washer finished/started via trigger messages (including the restart rewrite
  from `unavailable` ignored); reconnect sets/clears `ha_error`; sensors pushed with the right ids/attributes;
  daily use-soon sent once per day at 08:00 and not in quiet hours; `ha_setup` creates the dashboard only when
  missing and finds washer entities from a states list.
- Commits: `feat: the app listens to Home Assistant directly`; `feat: setup configures Home Assistant (washer, sidebar)`;
  `chore: remove the HA YAML package and hooks`.

## Task D: Layout that grows with the screen (desktop-friendly, imports first)

Breakpoints: phone (< 900 px: bottom tabs), tablet/laptop (900–1279 px: rail), desktop (≥ 1280 px: rail + wide
content). Content width is per page type, not one 720 px column:

- **Import review (the desktop workhorse)**: ≥ 1280 px two panes — left: the source (the uploaded PDF shown inline
  via `/import/{id}/source` in an `<object>`/`<iframe>`, or the recipe website opened in a link + its found photo),
  right: the review (title, servings, ingredient matching, table editor, Save). Each pane scrolls on its own;
  Save stays visible (sticky footer in the right pane). 900–1279 px: stacked, source collapsed into a "Show the
  source" disclosure. Import page itself: a large drop zone for PDFs (drag and drop, multiple files) on desktop.
- **Recipe page**: ≥ 1280 px the table gets the full width (up to ~1400 px); photo/servings in the title row;
  steps and macros in a side column beside "Add to plan" under the table.
- **Recipes list**: photo grid fills the width (auto-fill columns ~220 px).
- **Plan**: ≥ 1280 px the week is a 7-column grid (one column per day); smaller = today's stacked blocks.
- **Shopping**: ≥ 1280 px two columns (lists | Walmart cart and still-to-add).
- **Today**: ≥ 1280 px two columns (needs you now | tonight + coming up).
- **Chores, Supplies, Pantry, Me**: comfortable max width (~960 px) with multi-column card grids where cards repeat.
- Everything still works by keyboard, focus is visible, no horizontal page scroll at any width ≥ 320 px.
- Tests: page classes/structure for the two-pane review and the 7-day grid; the source route serves the PDF with
  the right content type and 404s for URL imports; no inline styles. Visual check at 390, 1024, 1440 and 1920 px.
- Commits: `feat: two-pane import review on desktop`; `feat: wide layouts for recipes, plan, shopping and today`.

Order: Task A (running) → Task B (photos, table-first recipe page) → Task C → Task D. B and D both touch the recipe
templates, so D goes last and builds on B's photo placement.
