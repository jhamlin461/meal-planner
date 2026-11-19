#!/usr/bin/env bash
# First-time setup (safe to re-run): asks for the keys, writes deploy/.env, starts the stack, sets up Grocy.
# Run inside the LXC:  bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")"

GROCY="http://localhost:9283"

get() { grep "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true; }
put() {  # replace KEY=... in .env, or append it
  K="$1" V="$2" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]}
    index($0, k"=")==1 {if (!d) print k"="v; d=1; next} {print}
    END{if (!d) print k"="v}' .env > .env.tmp && mv .env.tmp .env
}
# ask KEY "question" [secret] [required] — Enter keeps the current value
ask() {
  local key=$1 q=$2 secret=${3:-} required=${4:-} cur v shown
  cur=$(get "$key")
  if [[ -n $secret && -n $cur ]]; then shown="…${cur: -4}"; else shown=$cur; fi
  while :; do
    if [[ -n $secret ]]; then read -rsp "$q [${shown:-none}]: " v; echo; else read -rp "$q [${shown:-none}]: " v; fi
    v=${v//$'\r'/}; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"  # pasted CR / spaces
    [[ $v == \"*\" || $v == \'*\' ]] && v=${v:1:${#v}-2}                              # pasted quotes
    v=${v:-$cur}
    [[ -z $required || -n $v ]] && break
    echo "  This one is required."
  done
  put "$key" "$v"
}
# washer KEY "what": the one entity ha_setup found is used; none or several = ask (blank = no laundry reminders)
washer() {
  local found n
  found=$(grep "^$1=" <<<"$ha_out" | cut -d= -f2- || true)
  n=$(grep -c . <<<"$found" || true)
  if [[ $n == 1 ]]; then put "$1" "$found"; echo "$2: $found"; return; fi
  [[ $n == 0 ]] || echo "  Found several: $(tr '\n' ' ' <<<"$found")"
  ask "$1" "$2 (blank = no laundry reminders)"
}
ha_ok() { curl -fsS -o /dev/null -H "Authorization: Bearer $2" "${1%/}/api/"; }
grocy_ok() { curl -fsS -o /dev/null -H "GROCY-API-KEY: $1" "$GROCY/api/system/info"; }
# prints the HTTP status Claude gives this key (200 = good, 401 = wrong key, 000 = no internet)
anthropic_status() { curl -s -o /dev/null -w '%{http_code}' -H "x-api-key: $1" -H "anthropic-version: 2023-06-01" \
  https://api.anthropic.com/v1/models || true; }

command -v docker >/dev/null || { echo "Installing Docker…"; curl -fsSL https://get.docker.com | sh; }

[[ -f .env ]] || cp .env.example .env
chmod 600 .env

echo "Press Enter to keep the value in [brackets]."
ask TZ "Time zone (e.g. America/Chicago)" "" required
while :; do
  ask ANTHROPIC_API_KEY "Anthropic API key (console.anthropic.com -> API Keys)" secret required
  case $(anthropic_status "$(get ANTHROPIC_API_KEY)") in
    200) break ;;
    401|403) echo "  Claude rejected that key. Copy it again from console.anthropic.com -> API Keys." ;;
    *) echo "  Couldn't reach Claude to check the key (no internet?); keeping it."; break ;;
  esac
done
ask FDC_API_KEY "USDA FoodData Central key (DEMO_KEY works, slowly)" secret
ask WALMART_STORE_ID "Walmart store id (optional)"
ask SHOPPING_WEEKDAY "Shopping day, 0=Mon … 6=Sun" "" required

while :; do
  ask HA_URL "Home Assistant URL (e.g. https://homeassistant.your-tailnet.ts.net)" "" required
  ask HA_TOKEN "Home Assistant long-lived access token" secret required
  ha_ok "$(get HA_URL)" "$(get HA_TOKEN)" && break
  echo "  Home Assistant rejected that URL or token, try again."
done

docker compose up -d --build

echo -n "Waiting for Grocy"
for _ in $(seq 60); do curl -fsS -o /dev/null "$GROCY" 2>/dev/null && break; echo -n .; sleep 2; done; echo

key=$(get GROCY_API_KEY)
if [[ -z $key ]] || ! grocy_ok "$key"; then
  ip=$(hostname -I | awk '{print $1}')
  cat <<EOF

Grocy needs an API key (one time only):
  1. Open http://$ip:9283 and log in as admin / admin
  2. Change the password (user menu -> Change password)
  3. User menu -> Manage API keys -> Add, then copy the key
EOF
  while :; do
    read -rsp "Grocy API key: " key; echo
    grocy_ok "$key" && break
    echo "  Grocy rejected that key, try again."
  done
  put GROCY_API_KEY "$key"
fi

if command -v tailscale >/dev/null; then
  tailscale serve --bg 8000 >/dev/null   # HTTPS on the tailnet; idempotent
  url=$(tailscale serve status 2>/dev/null | grep -o 'https://[^ ]*' | head -n1)
fi

# From inside the app container (proving it reaches HA): find the washer, add the House sidebar dashboard.
echo "Setting up Home Assistant…"
rc=0
ha_out=$(docker compose run --rm -T -e APP_URL="${url:-}" mealplanner python -m app.ha_setup) || rc=$?
ha_out=${ha_out//$'\r'/}
grep -v '^WASHER_' <<<"$ha_out" | sed 's/^/  /' || true
if (( rc == 0 )); then
  washer WASHER_DONE_EVENT "Washer 'finished' event entity"
  washer WASHER_STATUS "Washer status sensor"
else
  echo "  Home Assistant setup didn't finish; fix that and re-run setup.sh."
fi

docker compose up -d --force-recreate mealplanner   # pick up the new .env
docker compose run --rm mealplanner python -m app.setup_grocy
echo "Done. Open the app at ${url:-the https://….ts.net address from 'tailscale serve status'}"
