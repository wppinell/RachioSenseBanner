#!/bin/bash
# fetch_rachio_zones.sh — one-off: dump current Rachio account to rachio_snapshot.json
# so Claude can rebuild the sensor→zone mapping after a controller swap.
# Safe to delete after use.

set -eu
cd "$(dirname "$0")"

# Prefer shell env; fall back to .env if present.
if [ -z "${RACHIO_API_KEY:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ -z "${RACHIO_API_KEY:-}" ]; then
  cat >&2 <<EOF
ERROR: RACHIO_API_KEY is not set.
  Either:
    export RACHIO_API_KEY="your_key_here"
  or create a .env file in $(pwd) containing:
    RACHIO_API_KEY=your_key_here
EOF
  exit 1
fi

echo "→ Fetching person id..."
PID=$(curl -sS -H "Authorization: Bearer $RACHIO_API_KEY" \
  https://api.rach.io/1/public/person/info \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "→ Fetching full account (person/$PID)..."
curl -sS -H "Authorization: Bearer $RACHIO_API_KEY" \
  "https://api.rach.io/1/public/person/$PID" \
  | python3 -m json.tool > rachio_snapshot.json

echo "✓ wrote rachio_snapshot.json ($(wc -l < rachio_snapshot.json) lines)"
echo "  You can now tell Claude: 'snapshot ready' — it can read the file directly."
