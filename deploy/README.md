# Deploying the meal planner

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

The repo has no remote, so send it over Tailscale. On the PC with the repo:

```
git bundle create meal-planner.bundle main
tailscale file cp meal-planner.bundle meal-planner:
```

Inside the LXC:

```
tailscale file get /root/
git clone /root/meal-planner.bundle /root/Recipes && cd /root/Recipes/deploy
cp .env.example .env        # fill in below (set TZ to your own time zone)
docker compose up -d
```

To update later: send a new bundle the same way, then in `/root/Recipes` run
`git pull /root/meal-planner.bundle main` and `docker compose up -d --build` in `deploy/`.

Find `<lxc-ip>` with `hostname -I` inside the LXC (use the LAN address).
Open Grocy once at `http://<lxc-ip>:9283` (login `admin`/`admin`): change the password, then create an API key
(user menu -> Manage API keys). This is the only time you use Grocy's own UI.

Put `GROCY_API_KEY`, `ANTHROPIC_API_KEY` and the rest in `deploy/.env` (see `.env.example`), then:

```
docker compose up -d                                     # pick up .env
docker compose run --rm mealplanner python -m app.setup_grocy
```

## Always use the HTTPS URL

Open the app at **`https://<lxc-hostname>.<tailnet>.ts.net`** (what `tailscale serve` prints), never at
`http://<ip>:8000`. The camera barcode scanner (getUserMedia) and the iPad screen wake lock only work in a
secure context, so over plain HTTP they silently fail.

Port 8000 stays published on the LAN only so Home Assistant can poll `/api/expiring`. The app has no login of
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
