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

`setup.sh` asks for your time zone, Anthropic key, USDA key, Walmart store id and shopping day, writes
`deploy/.env`, builds and starts the stack, then walks you through Grocy's one-time API key (open
`http://<lxc-ip>:9283`, log in `admin`/`admin`, change the password, user menu -> Manage API keys), checks the
key against Grocy and finishes the Grocy setup. Re-run it any time to change a key; Enter keeps the current
value.

To update later:

```
cd /root/meal-planner && git pull && cd deploy && docker compose up -d --build
```

## Always use the HTTPS URL

Open the app at **`https://<lxc-hostname>.<tailnet>.ts.net`** (what `tailscale serve` prints), never at
`http://<ip>:8000`. The camera barcode scanner (getUserMedia) and the iPad screen wake lock only work in a
secure context, so over plain HTTP they silently fail.

Port 8000 stays published on the LAN only so Home Assistant can poll `/api/expiring` and `/api/summary`. The app has no login of
its own: the LAN plus your tailnet is the trust boundary. Do not forward ports 8000 or 9283 from your router.

## 4. Backups

Add the LXC to a scheduled Proxmox backup (Datacenter -> Backup). That covers everything inside it, including
`deploy/data` (pending imports and recipe source PDFs) and `deploy/grocy` (the Grocy database). If you back
up files separately, include both directories.

## 5. Devices

- **Android phones:** install Tailscale, open the HTTPS URL in Chrome, menu -> **Install app**.
- **iPad:** install the Tailscale app, open the HTTPS URL in Safari -> Share -> **Add to Home Screen**.

## 6. Home Assistant

1. Optional (the expiring notification needs only the REST sensor below): the Grocy integration is a HACS custom
   component, not part of HA core. Install [HACS](https://hacs.xyz), add Grocy from it, then Settings -> Devices &
   services: URL `http://<lxc-ip>`, port `9283`, the API key.
2. Merge `ha/configuration.yaml` into HA's `configuration.yaml` (REST sensor `sensor.meal_planner_expiring`,
   polled every 30 minutes) and restart HA.
3. Merge `ha/automation.yaml` into your automations. Replace `<phone1>`/`<phone2>` with your notify service
   names: Developer Tools -> Actions, search `notify.mobile_app_`.

### House reminders and the sidebar

1. Copy `ha/house.yaml` to HA's `config/packages/house.yaml` (turn packages on as its header says), add
   `house_secret` to `secrets.yaml` (the same value as `HOOK_SECRET` in `deploy/.env`), replace `<lxc-ip>`, set
   the washer entities if yours differ, and restart HA. Its REST sensors read `/api/summary` (no secret).
2. In the app, open Me -> People and add each person (their Grocy user, HA person and phone).
3. Both phones: install the HA Companion app, allow notifications, stay on Tailscale, and set the app's
   External URL to `https://homeassistant.<tailnet>.ts.net` (HA is also on http, port 80). Pushes arrive
   anywhere; Done and Snooze taps need HA reachable, so the External URL is what makes them work away from home.
4. Sidebar: Settings -> Dashboards -> Add dashboard -> Webpage -> `https://<lxc-hostname>.<tailnet>.ts.net`,
   title **House**, icon `mdi:home-heart`.
5. Test scanning inside the HA app. If the camera is blocked there, Scan's **Open scanner in browser** link
   opens it in the browser.

## First run checks

- [ ] Scan one barcode on each phone.
- [ ] Import one EveryPlate PDF; check that packets became real measurements and spice blends were split into spices.
- [ ] Open the Walmart cart link.
- [ ] Add the iPad web clip.
- [ ] Confirm the HA notification fires (Developer Tools -> Actions: run the automation).

## Live Grocy integration test

The image has only the app (no tests or dev deps), so run it from the clone on the LXC with uv:

```
curl -LsSf https://astral.sh/uv/install.sh | sh    # once; then open a new shell
cd <repo>
GROCY_URL=http://localhost:9283/api GROCY_API_KEY=... uv run pytest tests/integration -v
```

It creates and deletes its own `zz-test-*` data and fails loudly if anything could not be cleaned up.
