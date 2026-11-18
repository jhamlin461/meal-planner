# House Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the meal planner into a house manager: a per-person Today screen, chores/tasks/upkeep with phone
reminders through Home Assistant, laundry and supply tracking, shown in HA's sidebar in a Midnight-themed redesign.

**Architecture:** Same FastAPI + Jinja2 + vanilla JS app. Grocy stays the record for chores, tasks, batteries
(upkeep) and stock; a new SQLite `HouseStore` under `DATA_DIR` holds people and reminder state; a background loop
in the web process sends HA notifications; HA calls back through secret-protected hook endpoints.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, httpx, sqlite3 (stdlib), vanilla JS, Grocy REST API, Home Assistant
REST API, pytest with fakes.

**Spec:** `docs/superpowers/specs/2026-11-18-house-manager-design.md` (builds on
`docs/superpowers/specs/2026-11-18-meal-planner-design.md`).

## Global Constraints

- No new runtime dependencies; SQLite through stdlib `sqlite3`, the loop through `threading`.
- Theme tokens, verbatim: background `#383C45`, surface `#434954`, rail `#2E333A`, action `#5294E2`,
  needs-you-now `#E45E65`, due-soon `#F4B400`, text `#FFFFFF`, muted `rgba(255,255,255,.68)`. Midnight is the
  default; light theme is a per-device choice.
- Type: Roboto Flex, self-hosted; heavy condensed setting only for the Today headline. Icons: Material Design
  Icons, self-hosted SVG sprite. No external CSS/JS/font requests at runtime.
- Controls never overlap at any width ≥ 320 px; buttons in a card split its width equally, min 48 px tall, text
  wraps inside the button. No all-caps labels; sentence case; buttons name their action ("Moved it", "Done").
- Quiet hours default `22:00-07:00` (env `QUIET_HOURS`); default remind-at `09:00`; laundry repeats every 15 min,
  everything else every 120 min.
- HA → app hooks require header `X-House-Secret: <HOOK_SECRET>`; wrong or missing → 403.
- Notification action ids: `HM:<done|snooze>:<kind>:<id>:<person_id>` with kind in `chore|task|battery`.
- Reminder keys: `chore:<id>`, `task:<id>`, `battery:<id>`. Audience: `person:<id>` or `home`.
- Grocy product group for supplies: `Household supplies`. Shopping lists: `Meal plan`, `Extras`, `Running low`.
- Existing tests keep passing; `create_app` gains keyword-only parameters with defaults so old call sites work.

## Review Focus

1. A reminder finished somewhere else (Grocy, an HA dashboard, the other phone) must stop nagging on the next
   loop and clear the notification on every phone it reached — Task 6 `test_done_elsewhere_clears_everywhere`.
2. Quiet hours that span midnight: nothing sends at 22:00–06:59; at 07:00 each held item sends once, not a burst
   of missed repeats — Task 6 `test_quiet_hours_hold_then_send_once`.
3. Home Assistant unreachable: the loop keeps running, Today shows the warning, and it clears when HA is back —
   Task 6 `test_ha_down_sets_flag_and_recovers`, Task 8 `test_today_shows_ha_warning`.
4. Double completion (both phones tap Done, or app and notification): the chore is executed once, so a
   garbage bag is consumed once — Task 7 `test_double_done_executes_once`.
5. Narrow screens: long names and two-button cards at 320 px wrap without overlap; the CfE table scrolls inside
   its own box — Task 12 Step 4 (visual check at 320, 390 and 1024 px).

---

### Task 1: Theme and navigation shell

**Files:**
- Modify: `app/templates/base.html`, `app/static/style.css`, `app/web.py`, `tests/test_web.py`
- Create: `app/static/fonts/RobotoFlex.woff2`, `app/static/icons.svg`, `scripts/build_icons.py`, `app/templates/_icon.html`

**Interfaces:**
- Produces: Jinja macro `icon(name, label=None)` in `_icon.html` rendering `<svg><use href="/static/icons.svg#<name>"></svg>`;
  `base.html` blocks `title`, `content`, `scripts`, plus `section` (one of `today|kitchen|home|scan`) and
  `subnav` (list of `(href, label)`); CSS custom properties `--bg --surface --rail --action --now --soon --text --muted`;
  CSS classes `.card`, `.card-actions` (equal-share full-width buttons), `.btn`, `.btn-primary`, `.rows`, `.row`,
  `.headline`, `.empty`.
- Routes: `GET /` (Today placeholder page until Task 8), `GET /home` (Home placeholder until Task 5),
  `POST /settings/theme` (`theme=dark|light`, sets cookie `theme`, 400-day max-age, redirects back).

- [ ] **Step 1: Write failing tests** in `tests/test_web.py`:
  `test_shell_has_four_destinations` (GET `/pantry`: contains links `/`, `/plan`, `/home`, `/scan` with labels
  Today, Kitchen, Home, Scan; the Kitchen one has `aria-current="page"`),
  `test_kitchen_subnav_lists_kitchen_pages` (GET `/plan`: contains `/plan`, `/pantry`, `/recipes`, `/shopping`),
  `test_theme_cookie_switches_to_light` (POST `/settings/theme` `theme=light` → 303, then GET `/pantry` has
  `data-theme="light"`; without cookie `data-theme="dark"`),
  `test_self_hosted_assets_served` (GET `/static/icons.svg` and `/static/fonts/RobotoFlex.woff2` → 200; base.html
  has no `http://` or `https://` stylesheet/script/font URLs).
- [ ] **Step 2:** Run `uv run pytest tests/test_web.py -q` — new tests FAIL.
- [ ] **Step 3: Implement.** `scripts/build_icons.py` downloads the listed MDI icons from
  `https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg/<name>.svg` into one `<symbol id="<name>">` sprite (icons:
  `white-balance-sunny silverware-fork-knife home-outline barcode-scan washing-machine broom cat trash-can-outline
  calendar-check battery-sync-outline basket-outline fridge-outline book-open-variant cart-outline account-outline
  check alarm-snooze plus pencil-outline chevron-right alert-circle-outline`); commit the generated sprite. Fetch
  Roboto Flex's latin variable woff2 from the `@fontsource-variable/roboto-flex` package on jsDelivr (full axes),
  commit it with `@font-face` in `style.css`. Phones (< 900 px): fixed bottom tab bar; ≥ 900 px: left rail with
  the active destination's subnav listed under it; content max-width 720 px. `html[data-theme]` from the cookie,
  dark tokens default, light tokens under `[data-theme="light"]`. Respect `prefers-reduced-motion`, visible
  `:focus-visible` rings.
- [ ] **Step 4:** Run `uv run pytest -q` — all PASS (existing tests unchanged).
- [ ] **Step 5:** Commit `feat: Midnight theme and Today/Kitchen/Home/Scan navigation shell`.

### Task 2: Settings and Home Assistant client

**Files:**
- Modify: `app/config.py`, `deploy/.env.example`, `deploy/setup.sh`, `tests/fakes.py`
- Create: `app/ha.py`, `tests/test_ha.py`

**Interfaces:**
- Produces: `Settings` fields `ha_url: str`, `ha_token: str`, `hook_secret: str`, `quiet_hours: tuple[time, time]`
  (from `HA_URL`, `HA_TOKEN`, `HOOK_SECRET`, `QUIET_HOURS` default `22:00-07:00`); new fields have defaults so
  existing `Settings(...)` calls in tests still work.
- `class HAError(Exception)`; `class HA(base_url: str, token: str, transport: httpx.BaseTransport | None = None)`:
  - `persons() -> dict[str, str]` — `person.*` entity id → friendly name (GET `/api/states`)
  - `home_states() -> dict[str, str]` — `person.*` → state (`home`, `not_home`, zone name)
  - `notify_services() -> list[str]` — names under the `notify` domain starting `mobile_app_` (GET `/api/services`)
  - `notify(service: str, title: str, message: str, tag: str, actions: list[tuple[str, str]]) -> None` — POST
    `/api/services/notify/<service>` (the legacy `notify.mobile_app_*` services, not `notify.send_message`) with
    `data: {tag, actions: [{action, title}], channel: "House reminders", ttl: 0, priority: "high"}`
  - `clear(service: str, tag: str) -> None` — message `clear_notification`, `data: {tag}`
  - Any transport error or non-2xx → `HAError`.
- `tests/fakes.py`: `FakeHA` with `persons_map`, `states`, `services`, `sent: list[dict]`, `cleared: list[tuple]`,
  `down: bool` (when True every call raises `HAError`).

- [ ] **Step 1: Write failing tests** `tests/test_ha.py` with `httpx.MockTransport`: `test_notify_posts_tag_and_actions`
  (body has `data.tag == "chore:3"`, `data.actions == [{"action": "HM:done:chore:3:1", "title": "Done"}]`, header
  `Authorization: Bearer t`), `test_clear_sends_clear_notification`, `test_home_states_only_persons`,
  `test_notify_services_mobile_only`, `test_http_error_raises_haerror` (500 and ConnectError);
  `tests/test_config.py::test_quiet_hours_parsed` (`"22:00-07:00"` → `(time(22), time(7))`).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3: Implement.** `setup.sh`: ask `HA_URL` (required) and `HA_TOKEN` (secret, required); check with
  `curl -fsS -H "Authorization: Bearer $t" "$url/api/"`, re-ask on failure; if `HOOK_SECRET` is empty generate
  one (`head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n'`) and print it with the line to add to HA's
  `secrets.yaml` (`house_secret: <value>`). Add the three keys and `QUIET_HOURS=22:00-07:00` to `.env.example`.
- [ ] **Step 4:** Run `uv run pytest -q` and re-run the stubbed `setup.sh` check from the deploy work (stub
  `docker`, `curl`, `hostname`, `tailscale` on PATH with `/c/...` paths; never the real binaries) — PASS.
- [ ] **Step 5:** Commit `feat: Home Assistant client and HA settings in setup`.

### Task 3: House store, people and "whose device is this?"

**Files:**
- Create: `app/house.py`, `app/templates/people.html`, `app/templates/who.html`, `tests/test_house.py`
- Modify: `app/grocy.py`, `tests/fakes.py`, `app/web.py`, `tests/test_web.py`

**Interfaces:**
- `@dataclass Person(id: int, name: str, grocy_user_id: int, ha_person: str, notify_service: str)`
- `@dataclass Reminder(key: str, audience: str | None, remind_at: str, repeat_min: int | None, snooze_until: datetime | None, last_sent: datetime | None, sent_to: list[str], verb: str | None)`
- `class HouseStore(path: Path)` (SQLite, created on first use, `check_same_thread=False`, one internal lock):
  `people() -> list[Person]`, `person(id) -> Person | None`, `save_person(name, grocy_user_id, ha_person, notify_service, id=None) -> int`,
  `delete_person(id)`, `reminder(key) -> Reminder` (defaults when absent: audience None, remind_at `"09:00"`,
  repeat_min None, nothing sent), `save_reminder(r: Reminder)`, `drop_reminder(key)`, `keys() -> list[str]`,
  `flag(name) -> str | None`, `set_flag(name, value: str | None)`.
- `Grocy.users() -> list[dict]` (GET `/users`), `Grocy.create_user(username: str, first_name: str) -> int`
  (POST `/users` with a random password; returns id by re-reading `/users`). FakeGrocy equivalents.
- `create_app(..., *, house: HouseStore | None = None, ha=None)`; when `house` is None it uses
  `HouseStore(settings.data_dir / "house.db")`; when `ha` is None and `settings.ha_url` is set it uses
  `HA(settings.ha_url, settings.ha_token)`, otherwise HA features show "Home Assistant isn't set up" instead of choices.
- Device identity: cookie `who` = person id or `kitchen` (400-day). Helper `current_who(request) -> Person | "kitchen" | None`.
- Routes: `GET /who` (choose: each person + "Kitchen (everyone)"), `POST /who` (`who=<id>|kitchen`), `GET /home/people`,
  `POST /home/people` (name, ha_person, notify_service; creates the Grocy user when new), `POST /home/people/{id}/delete`.
  The people form offers `ha.persons()` and `ha.notify_services()` as choices and pre-selects the obvious match
  (person whose name contains the typed name; for a chosen `person.*`, the notify service of the phone tracked by that
  person — HA's person state attributes list `device_trackers`; `device_tracker.<x>` ↔ `notify.mobile_app_<x>`).

- [ ] **Step 1: Write failing tests:** `test_store_roundtrip_people_and_reminders` (tmp_path; save, reload a new
  `HouseStore` on the same file, values persist), `test_reminder_defaults`, `test_flags`;
  web: `test_people_form_creates_grocy_user` (POST → FakeGrocy has a user named Josh, store has the person with the
  chosen `ha_person`/`notify_service`), `test_who_cookie_set_and_used` (POST `/who` `who=1` → cookie; GET `/home/people` marks Josh as this device),
  `test_people_form_lists_ha_choices` (FakeHA persons and services appear as options).
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest -q` — PASS.
- [ ] **Step 5:** Commit `feat: people, device identity and the house store`.

### Task 4: Grocy chores, tasks and upkeep client

**Files:**
- Modify: `app/grocy.py`, `tests/fakes.py`, `tests/test_grocy.py`, `tests/integration/test_grocy_live.py`

**Interfaces:**
- `Grocy.chores_status() -> list[dict]` (GET `/chores`: `chore_id`, `chore_name`, `next_estimated_execution_time`,
  `next_execution_assigned_to_user_id`, `last_tracked_time`),
  `Grocy.execute_chore(chore_id: int, done_by: int | None, skipped: bool = False) -> None` (POST `/chores/{id}/execute`
  `{"tracked_time": now, "done_by": ..., "skipped": ...}`),
  `Grocy.open_tasks() -> list[dict]` (GET `/tasks`), `Grocy.complete_task(task_id: int) -> None` (POST `/tasks/{id}/complete`),
  `Grocy.batteries_status() -> list[dict]` (GET `/batteries`: `battery_id`, `next_estimated_charge_time`, `last_tracked_time`),
  `Grocy.charge_battery(battery_id: int) -> None` (POST `/batteries/{id}/charge`).
  Chores, tasks and batteries are created/edited with the existing generic `create`/`update`/`delete`.
- FakeGrocy: same methods. `execute_chore` records an execution, recomputes the next due date from
  `period_type` (`daily` / `dynamic-regular` with `period_interval` days / `weekly` with `period_config` weekday
  names / `monthly` with `period_days`), advances `who-least-did-first` assignment across `assignment_config`
  user ids, and when `consume_product_on_chore_execution` is 1 calls `consume(product_id, product_amount)` —
  ignoring a "not enough stock" failure (chore still completes). Skipped executions move the date but consume nothing.

- [ ] **Step 1: Write failing tests:** `test_execute_chore_posts_done_by` (MockTransport body), `test_open_tasks_and_complete`,
  `test_battery_charge`; fake: `test_fake_chore_consumes_supply` (trash chore with product 5, amount 1 → stock 4→3),
  `test_fake_chore_with_no_stock_still_completes`, `test_fake_rotation_who_least_did_first` (users 1,2: after 1 does it twice
  and 2 once, next assigned is 2). Live test (skipped without Grocy): create chore with consume settings, execute, assert stock drops by 1 and next date moves; clean up.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `uv run pytest -q` PASS.
- [ ] **Step 5:** Commit `feat: Grocy chores, tasks and upkeep client`.

### Task 5: Home pages — chores, tasks, upkeep

**Files:**
- Create: `app/home.py`, `app/templates/home.html`, `app/templates/chore_form.html`, `app/templates/task_form.html`, `app/templates/upkeep_form.html`, `tests/test_home.py`
- Modify: `app/web.py`, `app/setup_grocy.py`

**Interfaces:**
- `app/home.py`: `STARTER_CHORES` (three dicts: *Scoop the litter boxes* daily rotate remind `18:00`; *Change the litter*
  `dynamic-regular` 14 days rotate, uses product named like "litter" if one exists; *Take out the trash* weekly rotate,
  remind `19:00`, uses 1 of a product named like "garbage bag"/"trash bag" if one exists),
  `add_starters(grocy, house, people) -> int` (skips names that already exist; returns how many added),
  `chore_fields(form) -> tuple[dict, Reminder fields]`, `task_fields(form)`, `upkeep_fields(form)`.
- `setup_grocy.bootstrap` also ensures task category `Household`, product group `Household supplies` and shopping
  list `Running low` (`LISTS` gains it).
- Routes (all redirect back to `/home` with a toast message):
  `GET /home` (three sections; empty state for chores has an "Add starter chores" button),
  `GET|POST /home/chores/new`, `GET|POST /home/chores/{id}`, `POST /home/chores/{id}/done`, `POST /home/chores/{id}/skip`,
  `POST /home/chores/{id}/delete`, same set for `/home/tasks` (done = complete) and `/home/upkeep` (done = charge),
  `POST /home/starters`.
- Chore form fields: name, schedule (daily / every N days / weekly on days / monthly on day), who (one person / take turns),
  remind at (time, default 09:00), repeat every (minutes, blank = default), uses supply (product select of the
  `Household supplies` group, optional) and amount. Task form: title, note, due date, who (person / whoever is home), remind at.
  Upkeep: name, every N days, remind at. The remind-at, repeat and audience values go in the `HouseStore` reminder for that key.
  "Done" credits the device's person (`done_by` = their `grocy_user_id`); kitchen device credits the assigned person.

- [ ] **Step 1: Write failing tests** (`tests/test_home.py`, TestClient with FakeGrocy/FakeHA/tmp HouseStore):
  `test_create_weekly_chore_with_supply` (POST form → Grocy chore `period_type=weekly`, `period_config="tuesday"`,
  `consume_product_on_chore_execution=1`, `product_amount=1`; reminder `remind_at="19:00"`),
  `test_take_turns_sets_who_least_did_first` (`assignment_config` = both people's grocy user ids),
  `test_task_whoever_home_stored_as_audience_home`, `test_done_credits_device_person`, `test_skip_moves_date_without_consuming`,
  `test_starters_added_once` (second POST adds 0), `test_starter_trash_links_garbage_bags_when_present`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS (`uv run pytest -q`).
- [ ] **Step 5:** Commit `feat: chores, tasks and upkeep pages`.

### Task 6: Reminder engine

**Files:**
- Create: `app/reminders.py`, `tests/test_reminders.py`
- Modify: `app/web.py` (start the loop)

**Interfaces:**
- `@dataclass Item(key: str, title: str, context: str, due: datetime, fire_at: datetime, audience: str, verb: str, repeat_min: int, icon: str)`
- `items(grocy, house, now: datetime) -> list[Item]` — every open chore, task and battery that is due on or before
  the end of `now`'s day plus 3 days; `due` from Grocy (date-only → that date); `fire_at` = due date at the
  reminder's `remind_at` (overdue → `due` itself); audience: reminder.audience, else the chore's
  `next_execution_assigned_to_user_id` / task's `assigned_to_user_id` mapped to `person:<id>`, else `home`;
  `repeat_min` = reminder.repeat_min or 120; `verb` = reminder.verb or "Done".
- `recipients(item, people, home_states) -> list[Person]` — `person:<id>` → that person (even when away);
  `home` → people whose `ha_person` state is `home`.
- `in_quiet(now: datetime, quiet: tuple[time, time]) -> bool` (spans midnight).
- `tick(grocy, house, ha, settings, now: datetime) -> None`:
  1. For keys in the store whose item is no longer open: clear the tag on each `sent_to` service, drop the reminder.
  2. If `in_quiet`: return.
  3. Get `home_states` once; on `HAError` set flag `ha_error` to a short message and return; else clear it.
  4. For each item with `fire_at <= now`, not snoozed, and (`last_sent` is None or `now - last_sent >= repeat_min`):
     notify each recipient (title, context, tag = key, actions Done/verb + "Snooze 15 min"), set `last_sent=now`,
     add services to `sent_to`. An `HAError` mid-way sets the flag and stops this tick.
  5. If quiet hours just ended, items held during them send once (step 4 handles this because `last_sent` is old).
- `start_loop(app_state_fn, interval=60) -> threading.Thread` — daemon thread calling `tick` with `datetime.now()`,
  logging and swallowing exceptions; started from `create_app` only when `settings.ha_url` is set (tests call `tick` directly).

- [ ] **Step 1: Write failing tests** (`tests/test_reminders.py`, FakeGrocy + FakeHA + tmp HouseStore, explicit `now`):
  `test_fires_at_remind_at_not_before` (chore due today, remind 18:00: tick at 17:59 sends nothing, 18:00 sends one to the assignee),
  `test_repeats_after_interval` (120 min default; laundry 15),
  `test_snooze_waits`,
  `test_whoever_home_targets_only_people_home`, `test_nobody_home_holds_until_someone_arrives`,
  `test_quiet_hours_hold_then_send_once` (overdue item; ticks every 10 min 21:50–07:10 → sent at 21:50, none 22:00–06:59, exactly one at 07:00),
  `test_done_elsewhere_clears_everywhere` (sent to two phones; Grocy marks complete; next tick clears both tags and drops the reminder),
  `test_ha_down_sets_flag_and_recovers` (FakeHA.down → flag set, no exception; back up → flag None and send happens).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: reminder engine with quiet hours, repeats and who's-home targeting`.

### Task 7: Notification actions and laundry hooks

**Files:**
- Modify: `app/web.py`, `app/reminders.py`, `tests/test_reminders.py`
- Create: `deploy/ha/house.yaml`, `tests/test_hooks.py`

**Interfaces:**
- `complete(grocy, house, ha, key: str, person: Person | None) -> bool` in `reminders.py` — executes the chore /
  completes the task / charges the battery only if it is still open (re-read from Grocy under a module lock),
  clears the tag on every `sent_to` service, drops the reminder; returns False when it was already done.
- `snooze(house, ha, key, minutes=15, now)` — sets `snooze_until`, clears the tags.
- Routes (require `X-House-Secret`): `POST /api/hook/action` JSON `{"action": "HM:done:chore:12:3"}`;
  `POST /api/events/washer-finished` → if `house.flag("laundry")` names a task that is still open, return `already-open`; otherwise
  create Grocy task "Move the laundry" (category Household, due today, no assignee) and reminder
  `audience="home", remind_at=<now HH:MM>, repeat_min=15, verb="Moved it"`, and `house.set_flag("laundry", "task:<id>")`; returns `{"status": "created"|"already-open"}`.
- In-app buttons reuse the same functions: `POST /today/{kind}/{id}/done`, `POST /today/{kind}/{id}/snooze` (cookie person).
  Task 5's `/home/.../done` routes switch to `complete()` here, so their notifications clear immediately too.
- `deploy/ha/house.yaml` (HA package): `rest_command.house_action` (POST to `http://<lxc-ip>:8000/api/hook/action`
  with `X-House-Secret: !secret house_secret`), automation "House: notification action" (trigger
  `mobile_app_notification_action`, condition action starts with `HM:`, calls the rest_command with the action),
  automation "House: washer finished": trigger state of `event.front_load_washer_notification`, condition
  `trigger.to_state.attributes.event_type == 'washing_is_complete'` and `trigger.from_state.state not in
  ['unavailable', 'unknown']` (HA restarts rewrite the event entity), calls `rest_command.house_washer_finished`;
  automation "House: washer started": `sensor.front_load_washer_current_status` to `initial` or `detecting` (from
  anything but `unavailable`/`unknown`) calls `rest_command.house_washer_started`. (Facts from the owner's HA, 8 cycles:
  states initial → detecting → running → rinsing → spinning → end, then `power_off` ~30 s later.)
- `POST /api/events/washer-started` (secret) → completes the open laundry task (the clothes were moved for the next
  load), crediting nobody; `{"status": "cleared"|"none-open"}`.

- [ ] **Step 1: Write failing tests:** `test_hook_requires_secret` (missing/wrong → 403), `test_action_done_completes_and_clears`,
  `test_double_done_executes_once` (two hook calls for a trash chore → one execution, garbage bags −1 once),
  `test_snooze_action`, `test_washer_finished_creates_one_task` (second call → `already-open`),
  `test_washer_started_clears_open_laundry`,
  `test_laundry_reminder_targets_people_home_every_15_min`, `test_ha_package_has_secret_and_automations` (PyYAML is
  not a dependency: assert the file text contains `!secret house_secret` and both automation aliases).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: notification actions and washer-finished laundry reminders`.

### Task 8: Today screen

**Files:**
- Create: `app/templates/today.html`, `app/today.py`, `tests/test_today.py`
- Modify: `app/web.py`

**Interfaces:**
- `today_view(grocy, house, who: Person | Literal["kitchen"], now) -> dict` with keys `now_items: list[Item]`
  (fired, not snoozed, for this person: their `person:` items plus `home` items while they're home; kitchen = all),
  `tonight: dict | None` (today's Dinner entry: recipe name, servings, leftover note from `week_view`),
  `coming: list[dict]` (`{"title", "when", "soon": bool}` — not-yet-fired items through +3 days, use-soon food from
  `volatile(settings.expiry_warn_days)`, Running low supplies), `ha_error: str | None`.
- `GET /` renders Today; no `who` cookie → redirect to `/who`. Headline: "N things need you" / "1 thing needs you" /
  "Nothing needs you" with the next item's title and time when nothing is due.
- Each now-item card: icon, title, context, `.card-actions` with Snooze 15 min and the verb (primary).

- [ ] **Step 1: Write failing tests:** `test_today_redirects_to_who_without_cookie`, `test_today_lists_my_due_items_only`,
  `test_today_home_items_only_when_home`, `test_kitchen_device_sees_everyone`, `test_today_headline_counts`,
  `test_today_empty_state_names_next_item`, `test_today_shows_tonight_dinner`, `test_today_shows_ha_warning`
  (flag set → text "Phone reminders aren't reaching Home Assistant"), `test_today_done_button_completes` (POST route from Task 7 → 303 to `/`).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: per-person Today screen`.

### Task 8b: Navigation — Today · Meals · Chores · Me (owner change, 2026-11-18)

**Files:**
- Create: `app/templates/me.html`
- Modify: `app/templates/base.html`, `app/static/style.css`, `app/web.py`, `app/templates/who.html`, `app/templates/scan.html` (section only), `tests/test_web.py`, `tests/test_home.py`

**Interfaces:**
- Bottom tabs / rail destinations, in order: **Today** (`/`, sun icon), **Meals** (`/plan`, silverware icon), **Chores**
  (`/home`, broom icon), **Me** (`/me`, account-outline icon). Scan is no longer a destination.
- Subnav strips: Meals = Plan `/plan`, Pantry `/pantry`, Recipes `/recipes`, Shopping `/shopping`, Scan `/scan`;
  Chores = Chores `/home`, Supplies `/home/supplies`, Scan `/scan?from=chores`. Internal section keys stay
  `kitchen`/`home` (URLs unchanged); only labels, icons and strips change. `/scan` shows the Meals strip, or the
  Chores strip with `?from=chores`; the Scan link is marked current in whichever strip shows it.
- `GET /home/supplies`: placeholder page in the Chores section ("Supplies you keep at home will show here.") until
  Task 9 fills it. `/home/people` moves under Me (section `me`), reachable from the Me page.
- `GET /me`: personal settings for this device — "This device is Josh's" with a Change link (`/who`), Theme
  (Dark / Light radio, posts to the existing `POST /settings/theme`, current one checked), and a People link.
  The footer "Use light theme" button is removed from every page. `/who` renders in the Me section.

- [ ] **Step 1: Write failing tests:** `test_tabs_are_today_meals_chores_me` (labels and hrefs in order, no Scan tab),
  `test_meals_strip_has_scan`, `test_chores_strip_has_supplies_and_scan` (`/scan?from=chores` shows the Chores strip),
  `test_me_page_theme_and_device` (radio reflects cookie; posting light → `data-theme="light"`), `test_no_footer_theme_button`.
  Update existing tests that assert the old labels/tabs.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `uv run pytest -q` PASS.
- [ ] **Step 5:** Commit `feat: Today, Meals, Chores and Me tabs; Scan inside Meals and Chores`.

### Task 9: Household supplies

**Files:**
- Modify: `app/ai.py`, `app/barcode.py`, `app/setup_grocy.py`, `app/shopping.py`, `app/web.py`, `app/static/app.js`, `app/templates/pantry.html`, tests (`test_barcode.py`, `test_shopping.py`, `test_web.py`), `tests/fakes.py`

**Interfaces:**
- `ProductGuess.category: Literal["food", "supply"] = "food"`; `PRODUCT_PROMPT` explains supply (non-food household
  consumables: trash bags, toilet paper, detergent, cat litter, cleaning products), which never expire.
- `LOCATIONS` gains `"Bathroom", "Laundry", "Under sink", "Garage", "Closet"`.
- `barcode._product_fields`: category supply → `product_group_id` = Household supplies, `default_best_before_days=-1`,
  location from the guess.
- `/api/scan` Add of a newly created supply with no `min_stock_amount` returns `{"status": "need_min", "product": ..., "unit": ...}`;
  app.js prompts "Keep at least how many?" (default 1 package) and POSTs `/api/products/{id}/min` `{"amount": n}`
  (validated > 0), then continues the add.
- `shopping.rebuild_running_low(grocy) -> dict[int, float]` — Grocy `volatile(...)["missing_products"]` (below
  `min_stock_amount`) into the `Running low` list, updated in place like `rebuild_meal_plan_list`; called wherever the
  Meal plan list is rebuilt. Bought handles Running low rows like Meal plan rows; Walmart cart and "Still to add" include them.
- The Supplies view is `GET /home/supplies` (Chores strip, created as a placeholder in Task 8b): each supply with amount, keep-at-least (editable),
  and a "Used one" button (`POST /pantry/{pid}/consume` amount 1 package or 1 unit).
- `FakeGrocy.volatile` computes `missing_products` from `min_stock_amount` vs stock (instead of the fixed result) when products have it set.

- [ ] **Step 1: Write failing tests:** `test_supply_scan_sets_group_and_never_expires`, `test_new_supply_asks_keep_at_least`
  (`need_min`; then POST min 2 → product `min_stock_amount=2`), `test_running_low_list_rebuilt_in_place`
  (stock 1 < min 2 → row amount 1; stock 3 → row removed; Extras untouched), `test_running_low_in_walmart_cart`,
  `test_supplies_view_used_one`, `test_trash_chore_drives_running_low` (execute trash chore until below min → Running low row).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: household supplies with keep-at-least and a Running low list`.

### Task 10: Home Assistant dashboards and sidebar

**Files:**
- Modify: `app/web.py`, `deploy/ha/house.yaml`, `deploy/README.md`
- Create: `tests/test_summary.py`

**Interfaces:**
- `GET /api/summary` (no secret, read-only like `/api/expiring`) →
  `{"people": {"<name>": {"now": n, "titles": [...]}}, "use_soon": n, "running_low": n, "laundry_open": bool, "ha_ok": bool}`.
- `house.yaml` adds REST sensors from `/api/summary` (one per person "needs you now", use soon, running low, laundry open)
  and an example dashboard card (entities card) in a commented block.
- README: "Home Assistant" section — copy `house.yaml` into HA `packages/`, add `house_secret`, set the washer entity,
  phone setup for reminders: both phones run the HA app with notifications allowed, stay on Tailscale, and set the
  app's External URL to `https://homeassistant.<tailnet>.ts.net` so action buttons work away from home (pushes arrive
  anywhere; taps need HA reachable). Then add the sidebar: Settings → Dashboards → Add dashboard → Webpage → `https://meal-planner.<tailnet>.ts.net`, title "House",
  icon `mdi:home-heart`; install the HA app on both phones and log in; test scanning inside the HA app (if the camera
  is blocked there, Scan's "Open scanner in browser" link is the fallback, added here: a link with `target="_blank"` on `/scan`).

- [ ] **Step 1: Write failing tests:** `test_summary_counts_per_person`, `test_summary_reports_laundry_and_ha_state`,
  `test_scan_has_open_in_browser_link`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: HA summary sensors, dashboard package and sidebar instructions`.

### Task 11: Kitchen redesign — Plan, Shopping, Recipes

**Files:**
- Modify: `app/templates/plan.html`, `shopping.html`, `recipes.html`, `recipe.html`, `_cfe_table.html`, `app/static/style.css`, `tests/test_web.py`

**Interfaces:** templates extend the Task 1 shell with `section="kitchen"`; use `.card`, `.card-actions`, `.rows`,
`icon()`. Behaviour and form field names unchanged (existing tests are the contract). The CfE table sits in a
horizontally scrolling container, themed with surface/rail colours and readable on the iPad.

- [ ] **Step 1: Write failing tests:** `test_kitchen_pages_use_shell` (each of `/plan`, `/shopping`, `/recipes`, `/recipes/<id>` has
  `data-section="kitchen"` and no inline `style=` attributes), `test_shopping_has_three_sections` (Meal plan, Running low, Extras headings in that order).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `uv run pytest -q` — PASS, including every pre-existing web test.
- [ ] **Step 5:** Commit `feat: redesigned plan, shopping and recipe pages`.

### Task 12: Kitchen redesign — Pantry, Scan, Import, Review, Table editor, Down page

**Files:**
- Modify: `app/templates/pantry.html`, `scan.html`, `import.html`, `review.html`, `table.html`, `_cfe_editor.html`, `down.html`, `app/static/style.css`, `app/static/app.js`, `tests/test_web.py`

**Interfaces:** as Task 11. Scan keeps the camera view full-width with the result as a card below it; the
`need_amount`/`need_min` prompts become an in-page sheet (not `window.prompt`) with numeric input and two
equal-share buttons.

- [ ] **Step 1: Write failing tests:** `test_remaining_pages_use_shell` (pantry, scan, import, review, table, down),
  `test_scan_prompts_are_in_page` (scan.html contains the sheet markup; app.js has no `prompt(`).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** `uv run pytest -q` — PASS.
- [ ] **Step 5: Visual check (controller or implementer with the browser pane):** run the app locally against
  FakeGrocy seed data (`uv run python -m tests.demo_server`, created here: FakeGrocy with two people, a laundry task,
  litter and trash chores, a planned dinner, a long-named supply below min) and screenshot Today, Home, Plan,
  Shopping, Pantry → Supplies, Scan, a recipe page at 320, 390 and 1024 px wide, in dark and light. Pass = no
  overlapping or clipped controls, every two-button card splits evenly, CfE table scrolls inside its box.
  Fix anything found and re-shoot.
- [ ] **Step 6:** Commit `feat: redesigned pantry, scan and import pages`.
