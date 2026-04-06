#!/bin/bash
# restart_banner.sh — Restart RachioSense dashboard services on the Pi and open browser

PI_HOST="wppinell@192.168.10.188"
DASHBOARD_URL="http://192.168.10.188:8090"

echo "🔄 Restarting RachioSense on Pi..."
ssh "$PI_HOST" "
  systemctl --user restart rachiosense.service
  sleep 3
  systemctl --user restart kiosk.service
  sleep 3
  systemctl --user is-active rachiosense.service && echo '✅ Server: running' || echo '❌ Server: failed'
  systemctl --user is-active kiosk.service && echo '✅ Kiosk:  running' || echo '❌ Kiosk:  failed'
"

echo "🌐 Opening dashboard in browser..."
open "$DASHBOARD_URL"
