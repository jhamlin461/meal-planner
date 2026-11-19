# Deploying the house manager

Runs on a Debian LXC on Proxmox with Docker Compose. Phones and the kitchen iPad reach it over Tailscale.

## 1. Create the LXC

Debian 12 container: 2 vCPU, 2 GB RAM, 16 GB disk, features `nesting=1,keyctl=1`.
Before first start, add to `/etc/pve/lxc/<id>.conf` on the Proxmox host (needed for Tailscale):

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

## 2. Install Docker and Tailscale (inside the LXC)

```
apt update && apt install -y curl git ca-certificates
curl -fsSL https://get.docker.com | sh
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

In the Tailscale admin console, enable **MagicDNS** and **HTTPS Certificates** (DNS page). Then:

```
tailscale serve --bg 8000
```

## 3. Start the stack

Inside the LXC (the repo is private: when git asks, the password is a GitHub fine-grained token with
**Contents: Read-only** on this repo; `credential.helper store` remembers it for later pulls):

```
git config --global credential.helper store
git clone https://github.com/jhamlin461/meal-planner.git /root/meal-planner
bash /root/meal-planner/deploy/setup.sh
```

`setup.sh` asks for your time zone, Anthropic key, USDA key, Walmart store id, shopping day and Home Assistant
URL and token (section 6), writes
`deploy/.env`, builds and starts the stack, then walks you through Grocy's one-time API key (open
`http://<lxc-ip>:9283`, log in `admin`/`admin`, change the password, user menu -> Manage API keys), checks the
key against Grocy and finishes the Grocy setup. Re-run it any time to change a key; Enter keeps the current
value.

To update later:

```
cd /root/meal-planner && git pull && cd deploy && docker compose up -d --build
```

## Always use the HTTPS URL

Open the app at **`https://<lxc-hostname>.<tailnet>.ts.net`** (what `tailscale serve` prints). That is the only way
in: port 8000 is published on the LXC's localhost only (for `tailscale serve`), so `http://<ip>:8000` doesn't answer
on the LAN. The camera barcode scanner (getUserMedia) and the iPad screen wake lock need that secure context anyway.

The app has no login of its own: your tailnet is the trust boundary. Do not forward port 9283 (Grocy, LAN only for
HA's Grocy integration) from your router.

## 4. Backups

Add the LXC to a scheduled Proxmox backup (Datacenter -> Backup). That covers everything inside it, including
`deploy/data` (pending imports and recipe source PDFs) and `deploy/grocy` (the Grocy database). If you back
up files separately, include both directories.

## 5. Devices

- **Android phones:** install Tailscale, open the HTTPS URL in Chrome, menu -> **Install app**.
- **iPad:** install the Tailscale app, open the HTTPS URL in Safari -> Share -> **Add to Home Screen**.

## 6. Home Assistant

The app talks to Home Assistant itself, with the token `setup.sh` asks for: nothing to copy into HA's config.

1. Both phones: install the HA Companion app, allow notifications, stay on Tailscale, and set the app's External URL
   to `https://homeassistant.<tailnet>.ts.net` (HA is also on http, port 80). Pushes arrive anywhere; Done and
   Snooze taps need HA reachable, so the External URL is what makes them work away from home.
2. Create a long-lived access token for the app. **The token must belong to an admin user**: HA only lets admins
   listen for phone taps, watch the washer, add dashboards and set sensors. Log in to HA as an admin, open your
   profile -> **Security** -> Long-lived access tokens -> Create.
3. Run `setup.sh` (re-run it if the app is already installed) and give it HA's URL and the token. After checking
   them it runs `python -m app.ha_setup` inside the app container (proving the container reaches HA), which:
   - finds the washer (an `event.*washer*` entity reporting `washing_is_complete`, and
     `sensor.*washer*current_status`) and stores them in `deploy/.env` as `WASHER_DONE_EVENT` / `WASHER_STATUS`;
     when it finds none or several it asks (`none` = no laundry reminders);
   - adds **House** (`mdi:home-heart`) to HA's sidebar: a Webpage dashboard showing the app's HTTPS URL (updated,
     not duplicated, when it already exists). If only this step fails, the washer settings are still saved.
4. In the app, open Me -> People and add each person (their Grocy user, HA person and phone).
5. Test scanning inside the HA app. If the camera is blocked there, Scan's **Open scanner in browser** link opens
   it in the browser.

What the app then does in HA:

- **Phone taps and the washer** arrive live over HA's WebSocket. While that connection is down, Today shows a
  warning; the app reconnects on its own.
- **Sensors**, pushed every minute and right after any Done, Snooze or Bought:
  `sensor.house_<person>_needs_you_now` (the name as saved under People, lowercased with `_` for spaces and
  punctuation, e.g. `sensor.house_joshua_needs_you_now`; attribute `titles`), `sensor.house_use_soon`,
  `sensor.house_running_low` and `binary_sensor.house_laundry_open`. Because the app sets them through HA's API they
  can't be edited in HA's UI, and after an HA restart they are missing until the app's next push (within a minute).
- **Food to use soon**: at 08:00 (after quiet hours, if they run later) each person with a phone gets one
  notification listing what's expiring, when anything is.

Optional: the Grocy integration is a HACS custom component, not part of HA core. Install [HACS](https://hacs.xyz),
add Grocy from it, then Settings -> Devices & services: URL `http://<lxc-ip>`, port `9283`, the API key.

### Moving from the YAML install

An earlier version was set up in HA with YAML (`packages/house.yaml`, `packages/meal_planner.yaml` and a `packages:`
line in `configuration.yaml`). In this order:

1. Update the app: `cd /root/meal-planner && git pull && bash deploy/setup.sh`. It reuses the existing **House**
   dashboard (`house-manager`) and says so if it still sees the old YAML automations. From now on the app is no
   longer reachable from the LAN, so the old YAML sensors and REST commands stop working: that is expected.
2. Delete both package files from HA's `config/packages/` folder and restart HA (the `packages:` line can stay;
   `house_secret` can go from `secrets.yaml`). Until then phone taps are handled twice (harmless, but noisy) and the
   old sensors clash with the app's `sensor.house_*` ones.

## First run checks

- [ ] Scan one barcode on each phone.
- [ ] Import one EveryPlate PDF; check that packets became real measurements and spice blends were split into spices.
- [ ] Open the Walmart cart link.
- [ ] Add the iPad web clip.
- [ ] HA -> Developer Tools -> States: the `sensor.house_*` entities exist and change within a minute of a Done.
- [ ] Laundry: when the washer finishes, the phones of whoever is home get "Move the laundry"; starting the next load
  clears it.
- [ ] The container (not just the LXC) reaches Home Assistant; this prints `200`:
  `docker compose exec mealplanner python -c "import os, httpx; print(httpx.get(os.environ['HA_URL'] + '/api/', headers={'Authorization': 'Bearer ' + os.environ['HA_TOKEN']}).status_code)"`
- [ ] Reminder round trip: add a task for whoever is home due today with Remind at a minute or two from now, wait
  for it on both phones, tap Done on one, and check it clears on the other within a minute.
- [ ] On each device, open the House sidebar in the HA app, pick who you are (Me -> This device), close and reopen
  the HA app, and check the sidebar still knows who you are (the `who` cookie survives in the HA webview).

## Live Grocy integration test

The image has only the app (no tests or dev deps), so run it from the clone on the LXC with uv:

```
curl -LsSf https://astral.sh/uv/install.sh | sh    # once; then open a new shell
cd <repo>
GROCY_URL=http://localhost:9283/api GROCY_API_KEY=... uv run pytest tests/integration -v
```

It creates and deletes its own `zz-test-*` data and fails loudly if anything could not be cleaned up.
