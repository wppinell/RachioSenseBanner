#!/bin/bash
# restart_local.sh — Restart local RachioSense server on Mac and open browser

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_URL="http://localhost:8090"

echo "🔄 Stopping any existing server..."
pkill -f "python.*server.py" 2>/dev/null
sleep 1

echo "🚀 Starting RachioSense server..."
cd "$SCRIPT_DIR"
nohup venv/bin/python server.py > /tmp/rachiosense.log 2>&1 &
SERVER_PID=$!

echo "⏳ Waiting for server to start..."
for i in {1..10}; do
  if curl -s "$DASHBOARD_URL" > /dev/null 2>&1; then
    echo "✅ Server running (PID $SERVER_PID)"
    break
  fi
  sleep 1
done

echo "🌐 Opening dashboard..."
open "$DASHBOARD_URL"
