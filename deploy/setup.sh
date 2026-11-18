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
    v=${v:-$cur}
    [[ -z $required || -n $v ]] && break
    echo "  This one is required."
  done
  put "$key" "$v"
}
grocy_ok() { curl -fsS -o /dev/null -H "GROCY-API-KEY: $1" "$GROCY/api/system/info"; }

command -v docker >/dev/null || { echo "Installing Docker…"; curl -fsSL https://get.docker.com | sh; }

[[ -f .env ]] || cp .env.example .env
chmod 600 .env

echo "Press Enter to keep the value in [brackets]."
ask TZ "Time zone (e.g. America/Chicago)" "" required
ask ANTHROPIC_API_KEY "Anthropic API key" secret required
ask FDC_API_KEY "USDA FoodData Central key (DEMO_KEY works, slowly)" secret
ask WALMART_STORE_ID "Walmart store id (optional)"
ask SHOPPING_WEEKDAY "Shopping day, 0=Mon … 6=Sun" "" required

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

docker compose up -d --force-recreate mealplanner   # pick up the new .env
docker compose run --rm mealplanner python -m app.setup_grocy
if command -v tailscale >/dev/null; then
  tailscale serve --bg 8000 >/dev/null   # HTTPS on the tailnet; idempotent
  url=$(tailscale serve status 2>/dev/null | grep -o 'https://[^ ]*' | head -n1)
fi
echo "Done. Open the app at ${url:-the https://….ts.net address from 'tailscale serve status'}"
