# RachioSenseBanner

A real-time irrigation dashboard for Raspberry Pi (or Mac) that combines Rachio smart irrigation data, SenseCraft soil moisture sensors, and live weather into a fullscreen 1920×1200 kiosk display.

---

## Features

- **Live moisture gauges** per zone, averaged across multiple sensors
- **Watering runtime rings** — 1-day (90 min = 100%) and 7-day (7 hours = 100%)
- **Zones sorted** from driest to wettest automatically
- **Photo-realistic weather tile** — sky renders to match current conditions (clear, partly cloudy, overcast, rain, sunset, night)
- **Dark theme** optimized for always-on kiosk display
- **30-minute polling** for all API sources (Rachio, SenseCraft, Open-Meteo)

---

## Requirements

- Python 3.9+
- Rachio smart irrigation controller
- SenseCraft/SenseCAP soil moisture sensors (model S2103 or similar)
- A Mac or Raspberry Pi on the same network

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/wppinell/RachioSenseBanner.git
cd RachioSenseBanner
```

### 2. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Where to find it |
|---|---|
| `RACHIO_API_KEY` | [app.rach.io](https://app.rach.io) → Account → Developer |
| `SENSECRAFT_API_KEY` | [sensecap.seeed.cc](https://sensecap.seeed.cc) → Account → API Access → **API ID** |
| `SENSECRAFT_API_SECRET` | Same page → **API Key** |
| `PORT` | Default `8090` (change if needed) |

### 4. Map sensors to zones

Start the server, then open `http://localhost:8090/api/sensors` to discover your sensor EUIs and zone IDs. Edit `config.json` to map them:

```json
{
  "sensor_zone_mapping": {
    "2CF7F1C062700084": "17a472bc-bbe5-4e7c-babb-fdf73a98b7aa"
  }
}
```

Multiple sensors can map to the same zone — their moisture readings are averaged.

### 5. Run

```bash
python3 server.py
```

Open `http://localhost:8090` in a browser.

---

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /api/data` | Full JSON payload for the dashboard |
| `GET /api/sensors` | Sensor EUIs + live moisture + zone list (for config.json setup) |
| `GET /api/debug/events` | Watering event zone names vs Rachio zone names (for runtime debugging) |
| `GET /api/health` | Service health check |

---

## Configuration

### `config.json`

```json
{
  "sensor_zone_mapping": {
    "<SENSOR_EUI>": "<RACHIO_ZONE_ID>"
  },
  "thresholds": {
    "critical": 20,
    "dry": 25,
    "high": 40
  }
}
```

### `.env`

```
RACHIO_API_KEY=...
SENSECRAFT_API_KEY=...
SENSECRAFT_API_SECRET=...
LATITUDE=33.4484
LONGITUDE=-112.0740
TIMEZONE=America/Phoenix
PORT=8090
```

---

## Raspberry Pi Kiosk Deployment

See `setup-kiosk.sh` for automated Pi setup. The script configures Chromium in kiosk mode at 1920×1200, auto-starts on boot, and points to the local Flask server.

---

## Architecture

```
dashboard.html  ←── GET /api/data (every 30s)
                          │
                     server.py
                    ┌──────────┐
                    │  Cache   │  (30-min TTL)
                    └────┬─────┘
          ┌──────────────┼──────────────┐
     Rachio API    SenseCraft API   Open-Meteo
   (zones/events)  (soil moisture)   (weather)
```

---

## License

MIT
