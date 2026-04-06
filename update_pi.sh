#!/bin/bash
# update_pi.sh — Pull latest from git and restart services on the Pi

PI_HOST="wppinell@192.168.10.188"

echo "📦 Pulling latest from git on Pi..."
ssh "$PI_HOST" "
  cd ~/rachiosense-dashboard
  git pull
  systemctl --user restart rachiosense.service
  sleep 3
  systemctl --user restart kiosk.service
  sleep 3
  systemctl --user is-active rachiosense.service && echo '✅ Server: running' || echo '❌ Server: failed'
  systemctl --user is-active kiosk.service && echo '✅ Kiosk:  running' || echo '❌ Kiosk:  failed'
"
echo "✅ Done."
