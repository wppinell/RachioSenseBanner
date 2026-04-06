"""
RachioSense Pi Dashboard Backend Server

Flask backend aggregating data from Rachio, SenseCraft, and Open-Meteo APIs.
"""

import os
import json
import logging
import threading
import time
import requests
import base64
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

from flask import Flask, jsonify, send_file
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Load .env
load_dotenv()

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s %(message)s'
)
logger = logging.getLogger('rachiosense')

# ── Configuration ──
RACHIO_API_KEY = os.getenv('RACHIO_API_KEY', '')
SENSECRAFT_API_KEY = os.getenv('SENSECRAFT_API_KEY', '')
SENSECRAFT_API_SECRET = os.getenv('SENSECRAFT_API_SECRET', '')
LATITUDE = float(os.getenv('LATITUDE', '33.4484'))
LONGITUDE = float(os.getenv('LONGITUDE', '-112.0740'))
ZIP_CODE = os.getenv('ZIP_CODE', '')
PORT = int(os.getenv('PORT', '8080'))
REFRESH_INTERVAL = int(os.getenv('REFRESH_INTERVAL', '1800'))

RACHIO_BASE = 'https://api.rach.io/1/public'
SENSECRAFT_BASE = 'https://sensecap.seeed.cc/openapi'
WEATHER_BASE = 'https://api.open-meteo.com/v1/forecast'

# Cache TTLs (seconds)
CACHE_TTL_DEVICES = 1800
CACHE_TTL_EVENTS  = 1800
CACHE_TTL_SENSORS = 1800
CACHE_TTL_WEATHER = 1800

# ── Weather mappings ──
WEATHER_EMOJI = {
    0: '☀️', 1: '🌤️', 2: '🌤️', 3: '⛅',
    45: '🌫️', 48: '🌫️',
    51: '🌦️', 53: '🌦️', 55: '🌦️',
    61: '🌧️', 63: '🌧️', 65: '🌧️',
    71: '❄️', 73: '❄️', 75: '❄️',
    80: '🌧️', 81: '🌧️', 82: '🌧️',
    95: '⛈️', 96: '⛈️', 99: '⛈️',
}
WEATHER_DESC = {
    0: 'Clear sky', 1: 'Mainly clear', 2: 'Partly cloudy', 3: 'Overcast',
    45: 'Foggy', 48: 'Foggy',
    51: 'Drizzle', 53: 'Drizzle', 55: 'Drizzle',
    61: 'Rain', 63: 'Moderate rain', 65: 'Heavy rain',
    71: 'Snow', 73: 'Snow', 75: 'Heavy snow',
    80: 'Rain showers', 81: 'Rain showers', 82: 'Heavy showers',
    95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm',
}

# ── Cache ──
class Cache:
    """Simple TTL-based cache."""
    def __init__(self):
        self.data = {}
        self.timestamps = {}

    def get(self, key):
        if key not in self.data:
            return None
        if key not in self.timestamps:
            return None
        age = time.time() - self.timestamps[key]
        if age > self.data.get(f'{key}_ttl', 0):
            del self.data[key]
            return None
        return self.data[key]

    def set(self, key, value, ttl):
        self.data[key] = value
        self.data[f'{key}_ttl'] = ttl
        self.timestamps[key] = time.time()

cache = Cache()

# ── Session with retries ──
def create_session_with_retries():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET', 'POST']
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# ── RachioAPI ──
class RachioAPI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = create_session_with_retries()
        self.person_id = None
        self.device_name = None
        self.api_remaining = None
        self.rate_limited_until = None

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _handle_rate_limit(self, response):
        remaining = response.headers.get('X-RateLimit-Remaining')
        if remaining:
            self.api_remaining = int(remaining)
        if response.status_code == 429:
            reset = response.headers.get('X-RateLimit-Reset')
            if reset:
                try:
                    from dateutil.parser import isoparse
                    self.rate_limited_until = isoparse(reset)
                except:
                    self.rate_limited_until = datetime.now(timezone.utc) + timedelta(minutes=5)
            else:
                self.rate_limited_until = datetime.now(timezone.utc) + timedelta(minutes=5)

    @property
    def is_rate_limited(self):
        if not self.rate_limited_until:
            return False
        return datetime.now(timezone.utc) < self.rate_limited_until

    def get_person_id(self):
        """GET /person/info → returns {"id": "...", ...} directly"""
        if self.person_id:
            return self.person_id
        cached = cache.get('rachio_person_id')
        if cached:
            self.person_id = cached
            return cached
        try:
            r = self.session.get(f'{RACHIO_BASE}/person/info', headers=self._headers(), timeout=15)
            self._handle_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            self.person_id = data.get('id')
            cache.set('rachio_person_id', self.person_id, CACHE_TTL_DEVICES)
            return self.person_id
        except Exception as e:
            logger.error(f'Rachio /person/info failed: {e}')
            return None

    def get_device_ids(self):
        """GET /person/{id} → returns {"id": "...", "devices": [{"id": "..."}]}"""
        person_id = self.get_person_id()
        if not person_id:
            return []
        cached = cache.get('rachio_device_ids')
        if cached:
            return cached
        try:
            r = self.session.get(f'{RACHIO_BASE}/person/{person_id}', headers=self._headers(), timeout=15)
            self._handle_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            ids = [d['id'] for d in data.get('devices', [])]
            cache.set('rachio_device_ids', ids, CACHE_TTL_DEVICES)
            return ids
        except Exception as e:
            logger.error(f'Rachio /person/{person_id} failed: {e}')
            return []

    def get_device(self, device_id):
        """GET /device/{id} → returns device object directly with zones, scheduleRules, etc."""
        cached = cache.get(f'rachio_device_{device_id}')
        if cached:
            return cached
        try:
            r = self.session.get(f'{RACHIO_BASE}/device/{device_id}', headers=self._headers(), timeout=15)
            self._handle_rate_limit(r)
            r.raise_for_status()
            device = r.json()
            self.device_name = device.get('name')
            cache.set(f'rachio_device_{device_id}', device, CACHE_TTL_DEVICES)
            logger.info(f'Fetched device: {device.get("name")}, {len(device.get("zones", []))} zones')
            return device
        except Exception as e:
            logger.error(f'Rachio /device/{device_id} failed: {e}')
            return None

    def get_events(self, device_id, days=7):
        """GET /device/{id}/event → returns a JSON array of event objects"""
        cached = cache.get(f'rachio_events_{device_id}')
        if cached is not None:
            return cached
        try:
            end_ms = int(datetime.now().timestamp() * 1000)
            start_ms = end_ms - (days * 86400 * 1000)
            r = self.session.get(
                f'{RACHIO_BASE}/device/{device_id}/event',
                headers=self._headers(),
                params={'startTime': start_ms, 'endTime': end_ms},
                timeout=15
            )
            self._handle_rate_limit(r)
            r.raise_for_status()
            events = r.json()  # Returns array directly
            if not isinstance(events, list):
                events = []
            cache.set(f'rachio_events_{device_id}', events, CACHE_TTL_EVENTS)
            logger.info(f'Fetched {len(events)} events for device {device_id}')
            return events
        except Exception as e:
            logger.error(f'Rachio events failed: {e}')
            return []

# ── SenseCraftAPI ──
class SenseCraftAPI:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = create_session_with_retries()

    def _auth_header(self):
        """Generate Authorization header for SenseCraft API."""
        credentials = f'{self.api_key}:{self.api_secret}'
        encoded = base64.b64encode(credentials.encode()).decode()
        return f'Basic {encoded}'

    def list_devices(self):
        """GET /list_devices → returns list of sensor devices."""
        cached = cache.get('sensecraft_devices')
        if cached is not None:
            return cached
        try:
            r = self.session.get(
                f'{SENSECRAFT_BASE}/list_devices',
                headers={'Authorization': self._auth_header(), 'Accept': 'application/json'},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            if data.get('code') == '0':
                devices = data.get('data', [])
                cache.set('sensecraft_devices', devices, CACHE_TTL_SENSORS)
                logger.info(f'Fetched {len(devices)} SenseCraft devices')
                return devices
            logger.warning(f'SenseCraft list_devices code: {data.get("code")} msg: {data.get("msg")}')
            return []
        except Exception as e:
            logger.error(f'SenseCraft list_devices failed: {e}')
            return []

    def get_latest_telemetry(self, eui):
        """GET /view_latest_telemetry_data?device_eui={eui} → latest moisture + temp reading."""
        cached = cache.get(f'sensecraft_{eui}')
        if cached is not None:
            return cached
        try:
            r = self.session.get(
                f'{SENSECRAFT_BASE}/view_latest_telemetry_data',
                headers={'Authorization': self._auth_header(), 'Accept': 'application/json'},
                params={'device_eui': eui},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            if data.get('code') == '0':
                result = data.get('data', [])
                cache.set(f'sensecraft_{eui}', result, CACHE_TTL_SENSORS)
                return result
            logger.warning(f'SenseCraft telemetry {eui} code: {data.get("code")} msg: {data.get("msg")}')
            return []
        except Exception as e:
            logger.error(f'SenseCraft telemetry for {eui} failed: {e}')
            return []

# ── WeatherAPI ──
class WeatherAPI:
    def __init__(self):
        self.session = create_session_with_retries()

    def get_weather(self, lat, lon):
        """Fetch weather forecast from Open-Meteo."""
        cached = cache.get('weather')
        if cached:
            return cached
        try:
            r = self.session.get(
                WEATHER_BASE,
                params={
                    'latitude': lat,
                    'longitude': lon,
                    'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m',
                    'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum',
                    'temperature_unit': 'fahrenheit',
                    'precipitation_unit': 'inch',
                    'timezone': 'auto',
                    'forecast_days': '7',
                },
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            cache.set('weather', data, CACHE_TTL_WEATHER)
            return data
        except Exception as e:
            logger.error(f'Weather API failed: {e}')
            return None

# ── Config loading ──
def load_config():
    """Load config.json with sensor-zone mapping and thresholds."""
    config_path = Path(__file__).parent / 'config.json'
    default_config = {
        'sensor_zone_map': {},
        'thresholds': {'critical': 20, 'dry': 25, 'high': 40},
        'zone_images': {}
    }
    if not config_path.exists():
        return default_config
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        # Support both old key and new key
        if 'sensor_zone_mapping' in cfg and 'sensor_zone_map' not in cfg:
            cfg['sensor_zone_map'] = cfg.pop('sensor_zone_mapping')
        return {**default_config, **cfg}
    except Exception as e:
        logger.error(f'Error loading config.json: {e}')
        return default_config

config = load_config()

# ── Moisture status ──
def moisture_status(pct, thresholds=None):
    """Return (status_str, hex_color) for a moisture percentage."""
    t = thresholds or config.get('thresholds', {})
    crit = t.get('critical', 20)
    dry = t.get('dry', 25)
    high = t.get('high', 40)
    if pct is None:
        return 'unknown', '#555970'
    if pct < crit:
        return 'critical', '#ef5350'
    if pct < dry:
        return 'dry', '#ffca28'
    if pct > high:
        return 'high', '#42a5f5'
    return 'ok', '#66bb6a'

# ── Parse watering events ──
def parse_watering_events(raw_events):
    """Parse raw Rachio events into structured watering events.

    Events have: type, subType, eventDate (epoch ms), summary.
    summary format: "Zone Name began watering..." or "Zone Name completed watering..."
    """
    zone_events = []
    for ev in raw_events:
        if ev.get('type') != 'ZONE_STATUS':
            continue
        sub = ev.get('subType', '')
        if sub not in ('ZONE_STARTED', 'ZONE_COMPLETED'):
            continue
        summary = ev.get('summary', '')
        # Extract zone name: text before "began", "started", "completed", "finished"
        zone_name = summary
        for sep in [' began ', ' started ', ' completed ', ' finished ']:
            if sep in summary:
                zone_name = summary.split(sep)[0].strip()
                break
        zone_events.append({
            'zone_name': zone_name,
            'sub_type': sub,
            'event_date': ev.get('eventDate', 0),
        })

    # Pair started/completed
    started = [e for e in zone_events if e['sub_type'] == 'ZONE_STARTED']
    completed = [e for e in zone_events if e['sub_type'] == 'ZONE_COMPLETED']

    watering_events = []
    for s in started:
        # Find the next completed event for this zone
        match = None
        for c in completed:
            if c['zone_name'] == s['zone_name'] and c['event_date'] > s['event_date']:
                if not match or c['event_date'] < match['event_date']:
                    match = c
        duration_sec = int((match['event_date'] - s['event_date']) / 1000) if match else 0
        if duration_sec >= 300:  # Only count runs >= 5 min
            watering_events.append({
                'zone_name': s['zone_name'],
                'start_ms': s['event_date'],
                'duration_sec': duration_sec,
            })

    return watering_events

def compute_zone_runtime(watering_events, zone_name, hours):
    """Sum runtime in minutes for a zone over past N hours."""
    cutoff_ms = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    zn = zone_name.lower().strip()
    total_sec = sum(
        e['duration_sec'] for e in watering_events
        if e['zone_name'].lower().strip() == zn and e['start_ms'] >= cutoff_ms
    )
    return total_sec // 60

# ── Schedule next run ──
def compute_next_run(schedule_rule):
    """Compute next run time from a schedule rule."""
    try:
        start_hour = schedule_rule.get('startHour', 5)
        start_minute = schedule_rule.get('startMinute', 0)
        days = schedule_rule.get('days', [])  # [0=Mon, 1=Tue, ..., 6=Sun]

        if not days:
            return None

        now = datetime.now()
        today_weekday = now.weekday()  # 0=Mon, 6=Sun

        # Find next scheduled day
        days_ahead = None
        for d in sorted(days):
            if d > today_weekday:
                days_ahead = d - today_weekday
                break

        if days_ahead is None:
            # No day found this week, use first day next week
            days_ahead = 7 + min(days) - today_weekday

        # Check if today is a scheduled day
        if today_weekday in days:
            # If we haven't passed the start time today, use today
            if now.hour < start_hour or (now.hour == start_hour and now.minute < start_minute):
                days_ahead = 0
            else:
                # We've passed it, find next day
                days_ahead = None
                for d in sorted(days):
                    if d > today_weekday:
                        days_ahead = d - today_weekday
                        break
                if days_ahead is None:
                    days_ahead = 7 + min(days) - today_weekday

        next_dt = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0) + timedelta(days=days_ahead)

        # Format result
        delta = (next_dt.date() - now.date()).days
        if delta == 0:
            day_label = 'Today'
        elif delta == 1:
            day_label = 'Tom'
        else:
            day_abbr = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            day_label = day_abbr[next_dt.weekday()]

        time_str = next_dt.strftime('%I:%M %p').lstrip('0')
        return f'{day_label} {time_str}'
    except Exception as e:
        logger.error(f'Schedule computation failed: {e}')
        return None

# ── Build zone data ──
def build_zone_data(rachio, sensecraft, device_id):
    device = rachio.get_device(device_id)
    if not device:
        return [], []

    zones = device.get('zones', [])
    raw_events = rachio.get_events(device_id) or []
    watering_events = parse_watering_events(raw_events)
    schedule_rules = device.get('scheduleRules', [])

    zone_data = []
    all_schedule_info = []

    for zone in zones:
        if not zone.get('enabled', True):
            continue

        zone_id = zone.get('id', '')
        zone_name = zone.get('name', f'Zone {zone.get("zoneNumber", "?")}')
        image_url = zone.get('imageUrl', '') or config.get('zone_images', {}).get(zone_id, '')

        # Find linked sensors
        sensors_list = []
        zone_moisture = None
        sensor_zone_map = config.get('sensor_zone_map', {})

        for eui, mapped_zone_id in sensor_zone_map.items():
            if mapped_zone_id != zone_id:
                continue
            if not sensecraft:
                continue
            telemetry = sensecraft.get_latest_telemetry(eui)
            if not telemetry:
                continue
            moisture_val = None
            for channel in telemetry:
                for point in channel.get('points', []):
                    if point.get('measurement_id') == '4103':
                        moisture_val = point.get('measurement_value')
            if moisture_val is not None:
                status, color = moisture_status(moisture_val)
                sensors_list.append({
                    'id': eui[-4:],  # Last 4 chars as short ID
                    'eui': eui,
                    'moisture': round(moisture_val, 1),
                    'color': color,
                })
                if zone_moisture is None:
                    zone_moisture = moisture_val
                else:
                    zone_moisture = (zone_moisture + moisture_val) / 2  # Average if multiple

        status, color = moisture_status(zone_moisture)

        # Compute runtimes
        r1d = compute_zone_runtime(watering_events, zone_name, 24)
        r7d = compute_zone_runtime(watering_events, zone_name, 168)

        # Find schedule for this zone
        sched_name = None
        next_run_str = None
        for rule in schedule_rules:
            if not rule.get('enabled', False):
                continue
            rule_zone_ids = [z.get('zoneId', z.get('id', '')) for z in rule.get('zones', [])]
            if zone_id in rule_zone_ids:
                sched_name = rule.get('name')
                next_run_str = compute_next_run(rule)
                if sched_name and next_run_str:
                    all_schedule_info.append({
                        'name': sched_name,
                        'next_run': next_run_str,
                        'zone_name': zone_name,
                    })
                break

        zone_data.append({
            'name': zone_name,
            'id': zone_id,
            'zoneNumber': zone.get('zoneNumber', 0),
            'imageUrl': image_url,
            'moisture': round(zone_moisture, 1) if zone_moisture is not None else None,
            'status': status,
            'statusColor': color,
            'sensors': sensors_list,
            'runtime1d': r1d,
            'runtime7d': r7d,
            'nextRun': next_run_str,
            'scheduleName': sched_name,
        })

    return zone_data, all_schedule_info

# ── Build alerts ──
def build_alerts(zones, schedule_info):
    alerts = []

    critical = [z for z in zones if z['status'] == 'critical']
    dry = [z for z in zones if z['status'] == 'dry']
    ok_count = sum(1 for z in zones if z['status'] in ('ok', 'high'))

    for z in critical:
        alerts.append({
            'level': 'crit',
            'icon': '🔴',
            'message': f'{z["name"]} at {z["moisture"]}% — critical'
        })
    for z in dry:
        alerts.append({
            'level': 'warn',
            'icon': '🟡',
            'message': f'{z["name"]} at {z["moisture"]}% — drying'
        })
    if ok_count > 0:
        alerts.append({
            'level': 'ok',
            'icon': '🟢',
            'message': f'{ok_count} of {len(zones)} zones healthy'
        })

    # Upcoming schedule runs
    for sched in schedule_info[:3]:
        alerts.append({
            'level': 'info',
            'icon': '📅',
            'message': f'Next run: {sched["name"]} · {sched["next_run"]}'
        })

    return alerts

# ── Build weather data ──
def build_weather(raw):
    if not raw:
        return None

    current = raw.get('current', {})
    daily = raw.get('daily', {})
    code = current.get('weather_code', 0)

    result = {
        'current': {
            'temp': round(current.get('temperature_2m', 0)),
            'humidity': current.get('relative_humidity_2m', 0),
            'wind': round(current.get('wind_speed_10m', 0)),
            'icon': WEATHER_EMOJI.get(code, '☁️'),
            'description': WEATHER_DESC.get(code, 'Unknown'),
        },
        'forecast': []
    }

    times = daily.get('time', [])
    codes = daily.get('weather_code', [])
    highs = daily.get('temperature_2m_max', [])
    lows = daily.get('temperature_2m_min', [])

    today = datetime.now().date()
    DAY_ABBR = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']

    for i in range(min(7, len(times), len(codes), len(highs), len(lows))):
        try:
            dt = datetime.strptime(times[i], '%Y-%m-%d').date()
        except:
            continue

        delta = (dt - today).days
        if delta == 0:
            day_label = 'TOD'
        elif delta == 1:
            day_label = 'TOM'
        else:
            day_label = DAY_ABBR[dt.weekday()]

        c = codes[i]
        result['forecast'].append({
            'day': day_label,
            'icon': WEATHER_EMOJI.get(c, '☁️'),
            'high': round(highs[i]),
            'low': round(lows[i]),
        })

    return result

# ── Build services status ──
def build_services(rachio, sensecraft, zones):
    sensor_count = len(config.get('sensor_zone_map', {}))
    return {
        'sensecraft': {
            'connected': sensecraft is not None and SENSECRAFT_API_KEY != '',
            'sensorCount': sensor_count,
            'lastSync': 'just now',
        },
        'rachio': {
            'connected': rachio is not None and RACHIO_API_KEY != '',
            'deviceName': rachio.device_name if rachio else None,
            'zoneCount': len(zones),
            'apiRemaining': rachio.api_remaining if rachio else None,
        }
    }

# ── Main aggregation ──
dashboard_data = {}
data_lock = threading.Lock()

def aggregate_all_data():
    logger.info('Aggregating dashboard data...')
    try:
        rachio = RachioAPI(RACHIO_API_KEY) if RACHIO_API_KEY else None
        sensecraft = SenseCraftAPI(SENSECRAFT_API_KEY, SENSECRAFT_API_SECRET) if SENSECRAFT_API_KEY and SENSECRAFT_API_SECRET else None
        weather_api = WeatherAPI()

        zones = []
        schedule_info = []
        device_id = None

        if rachio:
            device_ids = rachio.get_device_ids()
            if device_ids:
                device_id = device_ids[0]
                zones, schedule_info = build_zone_data(rachio, sensecraft, device_id)

        weather_raw = weather_api.get_weather(LATITUDE, LONGITUDE)

        return {
            'zones': zones,
            'alerts': build_alerts(zones, schedule_info),
            'weather': build_weather(weather_raw),
            'services': build_services(rachio, sensecraft, zones),
            'updatedAt': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f'Aggregation failed: {e}', exc_info=True)
        return {
            'zones': [],
            'alerts': [{'level': 'crit', 'icon': '❌', 'message': f'Dashboard error: {e}'}],
            'weather': None,
            'services': {},
            'updatedAt': datetime.now(timezone.utc).isoformat(),
        }

# ── Background refresh thread ──
RETRY_INTERVAL = 60  # seconds to wait before retrying after a failed/empty fetch

def background_refresh():
    global dashboard_data
    while True:
        # If zones are empty (API failure/rate limit), retry sooner
        with data_lock:
            has_zones = bool(dashboard_data.get('zones'))
        sleep_time = RETRY_INTERVAL if not has_zones else REFRESH_INTERVAL
        if not has_zones:
            logger.info(f'No zone data — retrying in {RETRY_INTERVAL}s')
        time.sleep(sleep_time)
        try:
            new_data = aggregate_all_data()
            with data_lock:
                dashboard_data = new_data
            if new_data.get('zones'):
                logger.info(f'Background refresh complete — {len(new_data["zones"])} zones')
            else:
                logger.warning('Background refresh complete — still no zones')
        except Exception as e:
            logger.error(f'Background refresh error: {e}')

# Initial load
dashboard_data = aggregate_all_data()

refresh_thread = threading.Thread(target=background_refresh, daemon=True)
refresh_thread.start()

# ── Flask routes ──
@app.route('/')
def serve_dashboard():
    """Serve the dashboard HTML file."""
    html_path = Path(__file__).parent / 'dashboard.html'
    return send_file(html_path, mimetype='text/html')

@app.route('/api/data')
def api_data():
    with data_lock:
        return jsonify(dashboard_data)

@app.route('/api/sensors')
def api_sensors():
    """List all SenseCraft sensors with EUIs and latest readings — use this to build config.json."""
    if not SENSECRAFT_API_KEY or not SENSECRAFT_API_SECRET:
        return jsonify({'error': 'SenseCraft not configured'}), 400
    sc = SenseCraftAPI(SENSECRAFT_API_KEY, SENSECRAFT_API_SECRET)
    devices = sc.list_devices() or []
    result = []
    for d in devices:
        eui = d.get('device_eui') or d.get('deviceEui', '')
        name = d.get('device_name') or d.get('deviceName', eui)
        telemetry = sc.get_latest_telemetry(eui) or []
        moisture = None
        for ch in telemetry:
            for pt in ch.get('points', []):
                if pt.get('measurement_id') == '4103':
                    moisture = pt.get('measurement_value')
        result.append({'eui': eui, 'name': name, 'moisture': moisture})
    return jsonify({
        'sensors': result,
        'zones': [{'id': z['id'], 'name': z['name'], 'zoneNumber': z['zoneNumber']} for z in dashboard_data.get('zones', [])],
        'hint': 'Map sensor EUIs to zone IDs in config.json under sensor_zone_map'
    })

@app.route('/api/debug/events')
def api_debug_events():
    """Show parsed watering events and zone names for diagnosing runtime mismatches."""
    with data_lock:
        zones = dashboard_data.get('zones', [])
    rachio = RachioAPI(RACHIO_API_KEY) if RACHIO_API_KEY else None
    if not rachio:
        return jsonify({'error': 'Rachio not configured'}), 400
    device_ids = rachio.get_device_ids()
    if not device_ids:
        return jsonify({'error': 'No Rachio devices'}), 400
    raw = rachio.get_events(device_ids[0]) or []
    events = parse_watering_events(raw)
    zone_names_in_events = sorted(set(e['zone_name'] for e in events))
    zone_names_in_data = [z['name'] for z in zones]
    return jsonify({
        'zone_names_in_events': zone_names_in_events,
        'zone_names_in_data': zone_names_in_data,
        'watering_events': events[:50],
    })

@app.route('/api/health')
def api_health():
    return jsonify({
        'status': 'ok',
        'uptime': time.time(),
        'rachio_configured': bool(RACHIO_API_KEY),
        'sensecraft_configured': bool(SENSECRAFT_API_KEY),
    })

def mask(val, show=4):
    """Mask a secret, showing only last N chars."""
    if not val: return ''
    return '•' * max(0, len(val) - show) + val[-show:]

def zip_to_latlon(zip_code: str):
    """Convert US ZIP code to lat/lon using Nominatim. Returns (lat, lon) or None."""
    try:
        url = 'https://nominatim.openstreetmap.org/search'
        params = {'postalcode': zip_code, 'country': 'US', 'format': 'json', 'limit': 1}
        headers = {'User-Agent': 'RachioSense/1.0'}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        data = resp.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        logger.warning(f'Geocoding failed for ZIP {zip_code}: {e}')
    return None

@app.route('/api/config', methods=['GET'])
def api_config_get():
    """Return current configuration (secrets masked)."""
    config_path = Path(__file__).parent / 'config.json'
    cfg = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
    mappings = []
    for eui, zone_id in cfg.get('sensor_zone_mapping', {}).items():
        comment = cfg.get('_comments', {}).get(eui, '')
        mappings.append({'eui': eui, 'zone_id': zone_id, 'comment': comment})
    return jsonify({
        'rachio_api_key': mask(RACHIO_API_KEY),
        'sensecraft_api_id': mask(SENSECRAFT_API_KEY),
        'sensecraft_api_secret': mask(SENSECRAFT_API_SECRET),
        'zip_code': ZIP_CODE,
        'latitude': LATITUDE,
        'longitude': LONGITUDE,
        'refresh_interval': REFRESH_INTERVAL,
        'sensor_mappings': mappings,
    })

@app.route('/api/config', methods=['POST'])
def api_config_post():
    """Save configuration to .env and config.json."""
    from flask import request
    data = request.get_json() or {}
    env_path = Path(__file__).parent / '.env'
    config_path = Path(__file__).parent / 'config.json'

    # Read existing .env lines
    lines = []
    if env_path.exists():
        with open(env_path) as f:
            lines = f.readlines()

    def set_env(lines, key, value):
        for i, line in enumerate(lines):
            if line.startswith(key + '='):
                lines[i] = f'{key}={value}\n'
                return lines
        lines.append(f'{key}={value}\n')
        return lines

    # Only update non-masked values (if user entered new value, not all bullets)
    def is_new_val(v):
        return v and '•' not in str(v)

    if is_new_val(data.get('rachio_api_key')):
        lines = set_env(lines, 'RACHIO_API_KEY', data['rachio_api_key'])
    if is_new_val(data.get('sensecraft_api_id')):
        lines = set_env(lines, 'SENSECRAFT_API_KEY', data['sensecraft_api_id'])
    if is_new_val(data.get('sensecraft_api_secret')):
        lines = set_env(lines, 'SENSECRAFT_API_SECRET', data['sensecraft_api_secret'])
    if data.get('zip_code'):
        zip_code = data['zip_code'].strip()
        lines = set_env(lines, 'ZIP_CODE', zip_code)
        coords = zip_to_latlon(zip_code)
        if coords:
            lines = set_env(lines, 'LATITUDE', str(coords[0]))
            lines = set_env(lines, 'LONGITUDE', str(coords[1]))
            logger.info(f'ZIP {zip_code} resolved to {coords[0]}, {coords[1]}')
        else:
            logger.warning(f'Could not geocode ZIP {zip_code}')
    if data.get('refresh_interval'):
        lines = set_env(lines, 'REFRESH_INTERVAL', data['refresh_interval'])

    with open(env_path, 'w') as f:
        f.writelines(lines)

    # Update sensor mappings in config.json
    if 'sensor_mappings' in data:
        cfg = {}
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
        new_mapping = {}
        new_comments = {}
        for m in data['sensor_mappings']:
            eui = m.get('eui', '').strip().upper()
            zone_id = m.get('zone_id', '').strip()
            comment = m.get('comment', '').strip()
            if eui and zone_id:
                new_mapping[eui] = zone_id
                if comment:
                    new_comments[eui] = comment
        cfg['sensor_zone_mapping'] = new_mapping
        cfg['_comments'] = new_comments
        with open(config_path, 'w') as f:
            json.dump(cfg, f, indent=2)

    return jsonify({'status': 'saved', 'message': 'Restart server to apply changes.'})

# ── Main ──
if __name__ == '__main__':
    logger.info('RachioSense Pi Dashboard starting')
    logger.info(f'Location: {LATITUDE}, {LONGITUDE}')
    logger.info(f'Rachio configured: {bool(RACHIO_API_KEY)}')
    logger.info(f'SenseCraft configured: {bool(SENSECRAFT_API_KEY)}')
    logger.info(f'Refresh interval: {REFRESH_INTERVAL}s')

    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
