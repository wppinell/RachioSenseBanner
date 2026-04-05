# Suggestions & Known Issues

Ideas for future improvements, known quirks, and deployment notes.

---

## Known Issues

### Garden Night schedule shows 0m runtime
Zones on the "Garden Night" schedule (Lettuce Garden, Tomato Garden) show zero watering runtime even when the schedule has run. The Rachio event log confirms no `ZONE_STARTED` events for these zones in the past 7 days — likely because Rachio's weather intelligence (rain skip, seasonal adjustment) is suppressing the schedule. Worth checking the Rachio app history to confirm when it last ran and whether a weather skip is active.

### Whole House Drip and Rose Bush — no sensors yet
Two sensors were ordered (April 2026). When they arrive:
1. Restart the server
2. Open `http://localhost:8090/api/sensors` to get the new EUIs
3. Add them to `config.json`

---

## Suggested Improvements

### Dashboard

- **Next watering countdown** — show time until next scheduled run on each tile, currently `nextRun` is computed but not prominently displayed
- **Last watered timestamp** — show "last watered 2 days ago" under each zone name
- **Moisture trend arrow** — compare current reading to previous reading and show ↑ / ↓ / → indicator
- **Zone images** — Rachio stores zone photos; they're already loaded as tile backgrounds but could be more prominent
- **Alert banner** — alerts are currently removed from the UI (replaced by weather tile); consider a dismissible banner at the top for critical moisture alerts
- **7-day forecast strip** — add a compact forecast row somewhere on screen (was in original alerts panel)

### Server

- **Webhook support** — Rachio supports webhooks for real-time zone start/stop events; this would allow instant runtime updates instead of waiting for the 30-min poll
- **Persistent cache** — write cache to disk (SQLite or JSON) so a server restart doesn't require waiting 30 min for fresh data
- **Historical moisture logging** — log sensor readings to SQLite over time; enables trend charts and min/max tracking
- **Configurable polling intervals** — expose `CACHE_TTL_*` as `.env` variables so they can be tuned without code changes
- **Pi auto-restart** — add a systemd service file so the Flask server restarts automatically after a Pi reboot

### Sensors

- **Temperature from SenseCraft** — the S2103 sensor also reports soil temperature (measurement_id `4102`); could display alongside moisture
- **Battery level** — SenseCraft API may expose battery status; useful for kiosk alerting before a sensor goes offline

### Pi Deployment

- **Static IP** — assign the Pi a static IP on the local network so the Mac Mini can reach `http://pi.local:8090` reliably
- **Reverse proxy** — put nginx in front of Flask for production-grade serving on the Pi
- **Screen brightness schedule** — dim the display at night using `vcgencmd` or `xrandr` to extend screen life
- **Watchdog** — use the Pi's hardware watchdog to reboot if the dashboard process crashes

---

## Completed / Won't Do

- ~~Unify Python server with RachioSense iOS Swift codebase~~ — not practical, keep separate
- ~~Pull functions from RachioSense project~~ — different languages, no shared code makes sense
