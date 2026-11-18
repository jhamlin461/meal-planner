# House Manager — Design

Date: 2026-11-18
Status: approved by owner 2026-11-18
Builds on: [2026-11-18-meal-planner-design.md](2026-11-18-meal-planner-design.md). Everything that spec says about
the kitchen (import, Cooking for Engineers tables, pantry, plan, shopping, Walmart) still holds unless this
document changes it.

## Intent and why

The meal planner becomes a **house manager**. The owner has ADHD: chores, laundry and supplies slip not because
nobody cares but because nobody is reminded at the right moment. The app's job is to remember for both people
and reach the right person at the right time, then get out of the way the moment the thing is done.

It lives inside Home Assistant, which already runs every smart device in the house, so there is one place to open
and one app on each phone.

## Goals

1. **One place.** The app appears as a "House" entry in Home Assistant's sidebar (HA phone app, HA on the iPad).
   Essentials also show on HA dashboards.
2. **Today screen** per person: what needs you now, tonight's dinner, what's coming up. Tick it off once and it's
   off everywhere.
3. **Chores**: repeating on a schedule, assigned to one person or rotating between both.
4. **Tasks**: one-off to-dos with a due date and an assignee.
5. **Upkeep**: things replaced on a cycle (filters, smoke-alarm batteries) — Grocy "batteries".
6. **Household supplies**: trash bags, toilet paper, detergent, litter, etc. tracked like food (scan in, scan out),
   with a "keep at least" amount; anything below it goes on the shopping list and into the Walmart cart.
7. **Reminders that reach the right person** as phone notifications through the Home Assistant app, with
   action buttons, repeating until done.
8. **Laundry left in the washer**: when the LG washer finishes, whoever is home is reminded to move it until
   someone confirms.
9. **Cat litter**: a rotating chore (daily scoop, full change every two weeks — the owner can change both).
10. **Modern redesign** of every screen, Home Assistant look, Midnight theme by default.

## Non-goals (for now)

- Appointments, bills, medication — not asked for. The task and reminder machinery should not block adding them.
- Logins or passwords. The tailnet and home LAN stay the trust boundary (meal-planner spec, Access and auth).
- Rebuilding the app as Home Assistant dashboards (rejected approach, below).
- Predicting supply usage from consumption rates. "Keep at least" thresholds are enough for v1.

## Anti-goals (failure even if it works)

- **Notification spam.** A reminder stops on every device the moment anyone completes it, in the app, on a
  notification button or on an HA dashboard. Nothing nags during quiet hours.
- **Reminders that silently don't arrive.** If HA can't be reached, the item still shows on Today and the app
  says notifications are failing.
- **Overlapping or cramped controls.** No control overlaps another at any width from 320 px up; tap targets are
  at least 48 px tall.
- **Losing kitchen features** in the redesign.

## Approaches considered

**Interface**
1. *Modern redesign of this app* — **chosen.** Phone-first, built for the HA sidebar.
2. *Make this app look like Grocy* — familiar but desktop-dense; on phones everything hides behind a menu.
3. *Put the features inside Grocy's own UI* via its custom JS hook — breaks on Grocy updates, brings back Grocy's
   menus, and its pages are desktop forms.

**Home Assistant**
1. *App in HA's sidebar* + 2. *essentials on HA dashboards* — **chosen together.** The app does the thinking
   and keeps the records; HA is where you open it and how it reaches you.
3. *Everything as HA dashboards, no app* — rejected: HA can't do recipe tables, import or scanning well.

## People and devices

- Two people, each a **Grocy user** (so chores and tasks can be assigned) and an **HA person** (so the app knows
  who's home and which phone to notify).
- **People settings** (Home → People): for each person, the Grocy user, the HA `person.*` entity and the HA
  notify service (`notify.mobile_app_<phone>`). Setup proposes the mapping from what HA reports; the owner
  confirms it.
- **"Whose device is this?"** asked once per device and remembered in a long-lived cookie; changeable from
  settings. The iPad can be set to "Kitchen (everyone)", which shows both people's items. No passwords.

## Structure and navigation

Phones: bottom tabs **Today · Kitchen · Home · Scan**. iPad and anything 900 px or wider: a left rail with the same
four destinations and their sub-pages listed under them.

- **Today** — per person (below).
- **Kitchen** — Plan, Pantry, Recipes, Shopping: today's pages, redesigned. Pantry gains a **Supplies** view.
- **Home** — Chores, Tasks, Upkeep, People. Add / edit / complete / skip for each.
- **Scan** — as today, now for supplies too.

## Today screen

Ordered by urgency, as in the approved mockup:

1. **Headline** with the count: "2 things need you" / "Nothing needs you" plus what's next.
2. **Needs you now**: overdue or due-now items for this person (or "whoever is home" items while they're home):
   laundry, chores, tasks, upkeep. Each shows a title, one line of context and its actions as full-width
   equal-share buttons (e.g. *Snooze 15 min* | *Moved it*; *Done*).
3. **Tonight**: the planned dinner and anything it needs ahead (thawing).
4. **Coming up**: due later today or in the next few days, food to use soon, supplies running low, as a plain
   list with tick circles.

## Reminders and notifications

- The app owns reminders: a background loop (every minute) finds items that are due and not done and decides
  who to tell:
  - assigned to a person → that person;
  - "whoever is home" → every person whose HA `person` state is `home`. If nobody is home, it waits and
    tells the first person to arrive.
- It sends through HA's REST API (`notify.<service>`) with actionable buttons: **Done** (or the item's own verb,
  e.g. *Moved it*) and **Snooze 15 min**. A notification `tag` per item replaces rather than stacks, and is
  cleared on every phone once done.
- HA turns button taps (`mobile_app_notification_action` events) into calls to the app (`rest_command`), via an
  automation shipped in `deploy/ha/`.
- **When it first fires** (owner's choice): each chore, task and upkeep item has its own **remind at** time of
  day, default **9:00 AM**, set on its edit screen (e.g. litter 6:00 PM, trash 7:00 PM the night before pickup).
  On the due date the item shows on Today from the start of the day and notifies at its remind-at time. Grocy
  keeps dates only; the remind-at time lives in the app's reminder store. Laundry fires immediately.
- **Repeats** until done: laundry every 15 minutes; chores, tasks and upkeep once when due, then every 2 hours.
  Per-item override.
- **Quiet hours** 22:00–07:00 (owner's call to change): no notifications; anything due is held until 07:00.
- Grocy is the record. Completing anywhere (app, notification, HA dashboard, Grocy itself) is picked up on the
  next loop and stops the reminder.
- If HA can't be reached, Today shows "Phone reminders aren't reaching Home Assistant" with what to check.

## Laundry

1. An HA automation watches the LG ThinQ washer's run state. Finished = the state becomes the "end/finished" value,
   **or** it goes from a running state (washing, rinsing, spinning) straight to off/unavailable, because this
   washer powers itself off at the end. Exact entity ids and state values are read from the owner's HA when
   building (the HA review session is recording them through a full cycle).
2. It calls the app (`POST /api/events/washer-finished`), which creates a "Move the laundry" task, due now,
   assigned to **whoever is home**, repeating every 15 minutes, buttons *Moved it* / *Snooze 15 min*.
3. A second finish while one is open does not create a duplicate.
4. If the dryer is smart, the same pattern covers "Empty the dryer" (only if HA exposes it).

## Chores, tasks, upkeep

- Stored in Grocy (chores, tasks, batteries) through its API, edited only in this app.
- Chores: schedule (daily, every N days, weekly on chosen days, monthly), assignment (one person / rotate), track
  "done" with who and when (Grocy chore execution). Skip and reschedule allowed.
- A chore can **use up a supply** each time it's done (Grocy's built-in "consume product on chore execution"):
  *Take out the trash* uses 1 garbage bag, *Change the litter* uses the litter amount. Set on the chore's edit
  screen (pick a supply and an amount). If the supply is out, the chore still completes and the supply shows
  as running low.
- Starter set offered on first run, editable: *Scoop the litter boxes* (daily, rotate), *Change the litter*
  (every 14 days, rotate, uses litter), *Take out the trash* (weekly, rotate, uses 1 garbage bag). The owner can
  decline or change any of them.
- Tasks: title, optional note, due date/time, assignee (person or whoever is home).
- Upkeep: name, cycle in days, last done; reminder when due.

## Household supplies

- Scan works for non-food: the AI product guess also returns a **category** (food / supply) and a home location.
  New locations: *Bathroom*, *Laundry*, *Under sink*, *Garage*, *Closet* (alongside the kitchen ones).
- Supplies never expire (Grocy best-before "never"), have a **keep at least** amount (asked on first scan,
  defaulting to 1 package) and live under Pantry → Supplies.
- **Use** on Scan (or a "Used one" button in Supplies) takes one off; running out is one tap.
- Below "keep at least" → a **Running low** section on the shopping list (alongside Meal plan and Extras) and in
  the Walmart cart, with the same Walmart item linking as food. Today's Coming up shows it.

## Home Assistant dashboards

- **Sidebar**: an HA "Webpage" dashboard pointing at the app's HTTPS address. Camera scanning inside the HA app's
  embedded page must be tested first; if it's blocked, Scan opens the app on its own for scanning and returns.
- **Dashboard essentials** via app REST sensors (`/api/summary`): items needing each person now, use-soon food,
  running-low supplies, washer status. Ticking chores and tasks from dashboards uses the Grocy HACS integration's
  entities/services (the app picks completions up from Grocy). Example cards ship in `deploy/ha/`.
- `/api/expiring` stays for the existing notification.

## Visual design

Approved mockup: `.superpowers/brainstorm/…/visual-style-v3.html` (local; screenshot sent to the owner).

- **Theme**: Midnight by default — background `#383C45`, surfaces `#434954`, rail/tab bar `#2E333A`, actions
  `#5294E2`, needs-you-now `#E45E65`, due-soon `#F4B400`, text `#FFFFFF`, muted `rgba(255,255,255,.68)`.
  A light theme is selectable per device.
- **Type**: Roboto Flex (self-hosted), the HA family, with a heavy condensed setting for the Today headline only.
- **Icons**: Material Design Icons (self-hosted subset), as in HA.
- **Hierarchy over decoration**: only "needs you now" gets colour; lists are divided rows, not identical cards.
  No all-caps labels.
- Buttons in a card split its full width equally, min 48 px tall; text wraps inside its button, never across.
- Responsive from 320 px; visible keyboard focus; respects reduced motion; Cooking for Engineers tables restyled
  to the theme and still readable on the iPad.

## Configuration additions

`setup.sh` also asks for `HA_URL` (e.g. `http://homeassistant.<tailnet>.ts.net`) and `HA_TOKEN` (an HA long-lived
access token), checks them against HA, then runs the People mapping.

## Error handling that matters

- HA down → reminders queue and Today warns (anti-goal 2). Grocy down → existing "Grocy is down" page.
- Notification action for an item already done → no-op, clears the notification.
- Duplicate washer events → one open laundry task.

## Decisions from the builder check (my calls, owner to check)

- **Reminder state** (snooze-until, last sent, repeat override, held-for-arrival, notification tags, HA-down queue)
  lives in a small SQLite file under `/data`, next to the pending imports. Grocy stays the record of what is
  due and what was done; the app reconciles every loop and drops state for items Grocy shows done.
- **Grocy starts empty** for chores, tasks and upkeep (fresh install), so there is nothing to migrate. Setup
  creates the two Grocy users from the names it asks for.
- **Supplies vs food** is a Grocy product group, *Household supplies*. Scan sets it from the AI's category;
  existing products stay food unless edited.
- **Rotation** uses Grocy's "who least did it first" assignment: whoever has done it fewer times is next, so if
  the other person does it, the balance shifts naturally. Only the assigned person is notified. Credit goes to
  the person whose phone tapped *Done*, or whose device ticked it in the app; ticks from HA dashboards or Grocy
  are credited to the assigned person.
- **App ↔ HA**: HA is VM 102 on the same Proxmox/LAN. HA calls the app at `http://<lxc-ip>:8000` (as it already
  does for `/api/expiring`); the app calls HA at `HA_URL`. Callbacks from HA (washer finished, done, snooze)
  carry a shared secret header that `setup.sh` generates and prints for HA's `secrets.yaml`; browser POSTs keep
  the existing cross-site guard.
- **Keep at least** is Grocy's `min_stock_amount`. The shopping rebuild gains a third list, *Running low*,
  computed from Grocy's missing-products data and rebuilt in place like *Meal plan*; *Extras* is never touched.
- **Build order**: theme and navigation shell first, so every new screen is built once in the new look; then the
  reminder loop and People; chores, tasks and upkeep; laundry; supplies; HA dashboards; finally re-skinning the
  existing kitchen pages.

## Left to the builder

- How the reminder loop runs (in-process task in the web app is fine).
- Exact REST shapes for `/api/summary` and action callbacks.
- Which MDI icons; illustration of empty states.
- How the light theme is chosen (per device setting).

## Owner calls made on their behalf (check these)

- Quiet hours 22:00–07:00; laundry repeats every 15 min, other items every 2 hours.
- Nobody home → hold until the first person arrives.
- The kitchen iPad as an "everyone" device.
- Starter litter chores: daily scoop and a 14-day change, rotating.
- Supply locations listed above; "keep at least" defaults to 1 package.
- The product name in HA's sidebar: "House".
