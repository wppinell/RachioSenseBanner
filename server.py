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
REFRESH_INTERVAL = int(os.getenv('REFRESH_INTERVAL', '21600'))  # 6 hours — Rachio schedule/zone data changes rarely

# Circuit-breaker floors for the Rachio API. These are enforced BEFORE any HTTP call and
# persist across process restarts via .rachio_state.json, so a crash-loop cannot burn the
# daily 3500-token budget. Even in a worst-case error scenario, this caps the call rate
# to at most ~12 Rachio requests/hour regardless of how many times the process restarts.
RACHIO_MIN_CALL_INTERVAL = int(os.getenv('RACHIO_MIN_CALL_INTERVAL', '300'))  # 5 min between any two calls
RACHIO_DAILY_CALL_CAP = int(os.getenv('RACHIO_DAILY_CALL_CAP', '200'))        # hard cap per 24h as a belt-and-suspenders guard

RACHIO_BASE = 'https://api.rach.io/1/public'
SENSECRAFT_BASE = 'https://sensecap.seeed.cc/openapi'
WEATHER_BASE = 'https://api.open-meteo.com/v1/forecast'

# Cache TTLs (seconds)
CACHE_TTL_DEVICES = 21600   # 6 hrs — zone config and schedule rules don't change often
CACHE_TTL_EVENTS  = 21600   # 6 hrs — only used by /api/debug/events
CACHE_TTL_SENSORS = 900     # 15 min — soil moisture does change through the day
CACHE_TTL_WEATHER = 1800    # 30 min — weather updates frequently

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

    def clear_prefix(self, prefix):
        """Remove all cache entries whose key starts with prefix."""
        keys = [k for k in self.data if k.startswith(prefix)]
        for k in keys:
            self.data.pop(k, None)
            self.timestamps.pop(k, None)

cache = Cache()

# ── ISO timestamp parser (stdlib-only, no dateutil dependency) ──
def _parse_iso(s):
    """Parse an ISO 8601 timestamp into a timezone-aware datetime using only the stdlib.
    Returns None if parsing fails. Handles trailing 'Z' (UTC) and both with/without
    microseconds. We use datetime.fromisoformat() which in Python 3.9+ accepts the
    format .isoformat() produces ('2026-04-08T03:15:11.279153+00:00')."""
    if not s:
        return None
    try:
        # Python 3.9's fromisoformat doesn't accept trailing 'Z' — swap for +00:00
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

# ── Disk-backed Rachio identity/rate-limit state ──
# These values survive server restarts so a crash-loop won't re-fetch
# identity data or hit the API while rate-limited.
_RACHIO_STATE_FILE = Path(__file__).parent / '.rachio_state.json'

def _load_rachio_state() -> dict:
    try:
        if _RACHIO_STATE_FILE.exists():
            with open(_RACHIO_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f'Failed to load rachio state: {e}')
    return {}

def _save_rachio_state(state: dict) -> None:
    try:
        with open(_RACHIO_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f'Failed to save rachio state: {e}')

_rachio_state = _load_rachio_state()
if _rachio_state:
    logger.info(f'Loaded Rachio state from disk: person_id={bool(_rachio_state.get("person_id"))}, '
                f'device_ids={len(_rachio_state.get("device_ids") or [])}, '
                f'rate_limited_until={_rachio_state.get("rate_limited_until")}')

# ── Session with retries ──
def create_session_with_retries():
    """Build an HTTP session with retry on transient errors only.

    IMPORTANT: 429 is deliberately NOT in the retry list. Retrying rate-limit
    responses multiplies API token consumption by 4× and accelerates exhaustion.
    When we see a 429, we back off at the application level instead.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],  # no 429!
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
        self.device_name = None
        # Hydrate identifiers, api_remaining, and rate-limit deadline from disk-backed state
        self.api_remaining = _rachio_state.get('api_remaining')
        self.person_id = _rachio_state.get('person_id')
        self._device_ids_cached = _rachio_state.get('device_ids') or []
        rl = _rachio_state.get('rate_limited_until')
        self.rate_limited_until = None
        if rl:
            try:
                self.rate_limited_until = _parse_iso(rl)
            except Exception:
                self.rate_limited_until = None
        # If the daily budget is known-exhausted, pin the wait to the next 5 PM local reset —
        # but ONLY if the stored deadline is still in the future. If the stored deadline has
        # already passed, the quota has reset (even if api_remaining is still 0 on disk),
        # so clear both fields and let the next call re-measure via X-RateLimit-Remaining.
        # Without this check, every restart after a reset would roll the deadline forward
        # another day and keep Rachio locked forever.
        now = datetime.now(timezone.utc)
        if self.rate_limited_until and self.rate_limited_until <= now:
            logger.info(f'Rachio rate-limit deadline {self.rate_limited_until.isoformat()} '
                        f'has passed — clearing state, quota has reset')
            self.rate_limited_until = None
            self.api_remaining = None
            _rachio_state['rate_limited_until'] = None
            _rachio_state['api_remaining'] = None
            # Also clear the circuit-breaker floor so the first call after a daily reset
            # can go through without waiting an extra 5 minutes.
            if _rachio_state.get('next_call_at'):
                _rachio_state['next_call_at'] = None
            _save_rachio_state(_rachio_state)
        elif self.api_remaining == 0:
            daily_reset = self._next_daily_reset()
            if not self.rate_limited_until or self.rate_limited_until < daily_reset:
                self.rate_limited_until = daily_reset
                _rachio_state['rate_limited_until'] = self.rate_limited_until.isoformat()
                _save_rachio_state(_rachio_state)

        # Circuit-breaker: persistent "no calls until this moment" floor. Survives restarts
        # so crash-loops cannot burn tokens. Hydrated from disk state.
        nca = _rachio_state.get('next_call_at')
        self.next_call_at = None
        if nca:
            try:
                self.next_call_at = _parse_iso(nca)
            except Exception:
                self.next_call_at = None

        # Daily call counter (belt-and-suspenders). Rolls at 00:00 UTC with the quota.
        self.calls_today = int(_rachio_state.get('calls_today') or 0)
        cd = _rachio_state.get('calls_day')
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if cd != today:
            self.calls_today = 0
            _rachio_state['calls_day'] = today
            _rachio_state['calls_today'] = 0
            _save_rachio_state(_rachio_state)

    def _headers(self):
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _persist_state(self):
        """Save identifiers, api_remaining, and rate-limit deadline to disk for restart survival."""
        _rachio_state['person_id'] = self.person_id
        _rachio_state['device_ids'] = self._device_ids_cached
        _rachio_state['api_remaining'] = self.api_remaining
        _rachio_state['rate_limited_until'] = (
            self.rate_limited_until.isoformat() if self.rate_limited_until else None
        )
        _rachio_state['next_call_at'] = (
            self.next_call_at.isoformat() if self.next_call_at else None
        )
        _rachio_state['calls_today'] = self.calls_today
        _rachio_state['calls_day'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        _save_rachio_state(_rachio_state)

    def _schedule_next_call(self, seconds):
        """Push the earliest-allowed-call time out by N seconds and persist immediately.
        The floor only moves forward — we never shorten a previously scheduled wait."""
        target = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        if not self.next_call_at or target > self.next_call_at:
            self.next_call_at = target
            self._persist_state()

    def _record_attempt(self):
        """Book-keeping before any HTTP call: bump the daily counter and persist.
        Called up-front so even an exception still counts toward the cap.
        Note: the crash-loop floor is NOT set here — it's set once per aggregate cycle
        by begin_cycle() so sequential calls in the same cycle don't block each other."""
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if _rachio_state.get('calls_day') != today:
            self.calls_today = 0
        self.calls_today += 1
        self._persist_state()

    def begin_cycle(self):
        """Arm the crash-loop floor at the start of an aggregate cycle. If the process
        crashes partway through, the next restart will see next_call_at in the future
        and skip Rachio until the floor clears — capping restart-driven traffic to at
        most 1 cycle per RACHIO_MIN_CALL_INTERVAL regardless of crash frequency."""
        self._schedule_next_call(RACHIO_MIN_CALL_INTERVAL)

    @property
    def can_call(self):
        """Gate for individual HTTP calls within an already-started aggregate cycle.
        Checks the hard per-day guards (daily quota + daily call cap) but NOT the
        crash-loop floor — once a cycle has begun, its sibling calls must be allowed
        to complete so we can produce a coherent snapshot."""
        if self.is_rate_limited:
            return False
        if self.calls_today >= RACHIO_DAILY_CALL_CAP:
            return False
        return True

    @property
    def can_begin_cycle(self):
        """Gate for starting a new aggregate cycle. Stricter than can_call — also
        enforces the persistent min-interval floor that protects against crash-loops
        between processes."""
        if not self.can_call:
            return False
        if self.next_call_at and datetime.now(timezone.utc) < self.next_call_at:
            return False
        return True

    def _block_reason(self):
        if self.is_rate_limited:
            return f'rate-limited until {self.rate_limited_until}'
        if self.calls_today >= RACHIO_DAILY_CALL_CAP:
            return f'daily call cap reached ({self.calls_today}/{RACHIO_DAILY_CALL_CAP})'
        if self.next_call_at and datetime.now(timezone.utc) < self.next_call_at:
            return f'min-interval floor until {self.next_call_at.isoformat()}'
        return 'unknown'

    def _record_usage_sample(self):
        """Append a (timestamp, api_remaining) sample to the persistent ring buffer.
        Used to compute tokens-per-hour usage rate. Samples older than 24h are pruned."""
        if self.api_remaining is None:
            return
        now = datetime.now(timezone.utc)
        samples = _rachio_state.get('usage_samples') or []
        # Skip the append if the newest sample already has the same api_remaining value —
        # no point storing duplicate readings.
        if samples and samples[-1][1] == self.api_remaining:
            return
        samples.append([now.isoformat(), self.api_remaining])
        # Prune anything older than 24h and cap total length at 500 entries
        cutoff = now - timedelta(hours=24)
        samples = [s for s in samples if _parse_iso(s[0]) and _parse_iso(s[0]) >= cutoff][-500:]
        _rachio_state['usage_samples'] = samples

    def tokens_per_hour(self):
        """Compute the average token consumption rate over the most recent rolling
        window (up to 1 hour). Returns None if we don't have enough data yet."""
        samples = _rachio_state.get('usage_samples') or []
        if len(samples) < 2:
            return None
        try:
            parsed = []
            for s in samples:
                t = _parse_iso(s[0])
                if t is None:
                    continue
                parsed.append((t, int(s[1])))
            if len(parsed) < 2:
                return None
        except Exception:
            return None
        now = datetime.now(timezone.utc)
        newest_t, newest_v = parsed[-1]
        hour_ago = now - timedelta(hours=1)
        # Prefer the last sample that's at least an hour old as the start of the window.
        older = [p for p in parsed if p[0] <= hour_ago]
        if older:
            oldest_t, oldest_v = older[-1]
        else:
            oldest_t, oldest_v = parsed[0]
        dt_hours = (newest_t - oldest_t).total_seconds() / 3600
        if dt_hours < 1/60:  # less than ~1 minute of data — not yet meaningful
            return None
        delta = oldest_v - newest_v  # api_remaining decreases over time
        if delta < 0:  # quota reset happened inside the window — skip
            return None
        # If the window is shorter than an hour, extrapolate to an hourly rate
        rate = delta / dt_hours
        return round(rate, 1)

    @staticmethod
    def _next_daily_reset():
        """Rachio's daily quota resets at 5 PM Phoenix local (00:00 UTC)."""
        now = datetime.now(timezone.utc)
        next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return next_reset

    def _handle_rate_limit(self, response):
        remaining = response.headers.get('X-RateLimit-Remaining')
        if remaining:
            try:
                self.api_remaining = int(remaining)
                _rachio_state['api_remaining'] = self.api_remaining
                self._record_usage_sample()
                _save_rachio_state(_rachio_state)
            except ValueError:
                pass
        if response.status_code == 429:
            reset = response.headers.get('X-RateLimit-Reset')
            parsed_reset = _parse_iso(reset) if reset else None
            if parsed_reset:
                self.rate_limited_until = parsed_reset
            else:
                self.rate_limited_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            # If the daily budget is exhausted, never retry before the next 5 PM local reset
            if self.api_remaining == 0:
                daily_reset = self._next_daily_reset()
                if self.rate_limited_until < daily_reset:
                    self.rate_limited_until = daily_reset
            logger.warning(f'Rachio rate-limited until {self.rate_limited_until}')
            self._persist_state()

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
        if not self.can_call:
            logger.warning(f'Rachio circuit-breaker open ({self._block_reason()}), skipping person_id fetch')
            return None
        self._record_attempt()
        try:
            r = self.session.get(f'{RACHIO_BASE}/person/info', headers=self._headers(), timeout=15)
            self._handle_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            self.person_id = data.get('id')
            cache.set('rachio_person_id', self.person_id, CACHE_TTL_DEVICES)
            self._persist_state()
            return self.person_id
        except Exception as e:
            logger.error(f'Rachio /person/info failed: {e}')
            return None

    def get_device_ids(self):
        """GET /person/{id} → returns {"id": "...", "devices": [{"id": "..."}]}"""
        if self._device_ids_cached:
            return self._device_ids_cached
        person_id = self.get_person_id()
        if not person_id:
            return []
        cached = cache.get('rachio_device_ids')
        if cached:
            self._device_ids_cached = cached
            return cached
        if not self.can_call:
            logger.warning(f'Rachio circuit-breaker open ({self._block_reason()}), skipping device_ids fetch')
            return []
        self._record_attempt()
        try:
            r = self.session.get(f'{RACHIO_BASE}/person/{person_id}', headers=self._headers(), timeout=15)
            self._handle_rate_limit(r)
            r.raise_for_status()
            data = r.json()
            ids = [d['id'] for d in data.get('devices', [])]
            cache.set('rachio_device_ids', ids, CACHE_TTL_DEVICES)
            self._device_ids_cached = ids
            self._persist_state()
            return ids
        except Exception as e:
            logger.error(f'Rachio /person/{person_id} failed: {e}')
            return []

    def get_device(self, device_id):
        """GET /device/{id} → returns device object directly with zones, scheduleRules, etc."""
        cached = cache.get(f'rachio_device_{device_id}')
        if cached:
            return cached
        if not self.can_call:
            logger.warning(f'Rachio circuit-breaker open ({self._block_reason()}), skipping device fetch for {device_id}')
            return None
        self._record_attempt()
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
        if not self.can_call:
            logger.warning(f'Rachio circuit-breaker open ({self._block_reason()}), skipping events fetch for {device_id}')
            return []
        self._record_attempt()
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
        """GET /view_latest_telemetry_data?device_eui={eui} → latest moisture + temp reading.

        Falls back to last known reading if the API is rate-limited or unreachable,
        so moisture values remain visible on the dashboard during SenseCraft outages.
        """
        global _last_good_telemetry, _last_sensecraft_sync
        cached = cache.get(f'sensecraft_{eui}')
        if cached is not None:
            if not _last_sensecraft_sync:
                _last_sensecraft_sync = datetime.now(timezone.utc).isoformat()
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
                _last_good_telemetry[eui] = result  # save for stale fallback
                _last_sensecraft_sync = datetime.now(timezone.utc).isoformat()
                return result
            logger.warning(f'SenseCraft telemetry {eui} code: {data.get("code")} msg: {data.get("msg")}')
        except Exception as e:
            logger.error(f'SenseCraft telemetry for {eui} failed: {e}')

        # Fall back to last known reading rather than returning nothing
        if eui in _last_good_telemetry:
            logger.warning(f'SenseCraft {eui[-4:]} — using stale telemetry')
            if not _last_sensecraft_sync:
                _last_sensecraft_sync = datetime.now(timezone.utc).isoformat()
            return _last_good_telemetry[eui]
        return []

# ── WeatherAPI ──
_WEATHER_CACHE_FILE = Path(__file__).parent / '.weather_cache.json'

def _load_weather_cache() -> Optional[dict]:
    try:
        if _WEATHER_CACHE_FILE.exists():
            with open(_WEATHER_CACHE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f'Could not load weather cache: {e}')
    return None

def _save_weather_cache(data: dict) -> None:
    try:
        with open(_WEATHER_CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f'Could not save weather cache: {e}')

def _nws_desc_to_code(desc: str) -> int:
    """Map NWS shortForecast strings to WMO weather codes."""
    if not desc:
        return 0
    d = desc.lower()
    if 'thunder' in d or 'storm' in d:
        return 95
    if 'snow' in d or 'flurr' in d or 'sleet' in d:
        return 73
    if 'heavy rain' in d or 'heavy shower' in d:
        return 82
    if 'shower' in d or 'rain' in d:
        return 80
    if 'drizzle' in d:
        return 53
    if 'fog' in d or 'mist' in d or 'haze' in d:
        return 45
    if 'overcast' in d:
        return 3
    if 'mostly cloudy' in d or 'broken' in d:
        return 3
    if 'partly' in d or 'mostly sunny' in d or 'few clouds' in d:
        return 2
    if 'cloud' in d:
        return 2
    if 'sunny' in d or 'clear' in d or 'fair' in d:
        return 0
    return 1

class WeatherAPI:
    def __init__(self):
        self.session = create_session_with_retries()
        # Hydrate in-memory cache from disk so restarts survive upstream outages
        disk = _load_weather_cache()
        if disk:
            cache.set('weather', disk, CACHE_TTL_WEATHER)
            logger.info('Hydrated weather cache from disk')

    def _fetch_open_meteo(self, lat, lon):
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
            timeout=10
        )
        r.raise_for_status()
        return r.json()

    def _fetch_nws(self, lat, lon):
        """Fetch from NWS (api.weather.gov), return in Open-Meteo-compatible shape."""
        headers = {
            'User-Agent': 'RachioSenseBanner/1.0 (contact: wppinell@gmail.com)',
            'Accept': 'application/geo+json',
        }
        # Step 1: grid point lookup (gives forecast URL + nearest obs station URL)
        p = self.session.get(
            f'https://api.weather.gov/points/{lat},{lon}',
            headers=headers, timeout=10
        )
        p.raise_for_status()
        points = p.json()
        fc_url = points['properties']['forecast']
        obs_stations_url = points['properties']['observationStations']

        # Step 2: forecast (day/night period pairs)
        f = self.session.get(fc_url, headers=headers, timeout=10)
        f.raise_for_status()
        periods = f.json()['properties']['periods']

        # Step 3: current observation from nearest station
        temp_f = 0
        humidity_pct = 0
        wind_mph = 0
        current_code = _nws_desc_to_code(periods[0].get('shortForecast', '') if periods else '')
        try:
            s = self.session.get(obs_stations_url, headers=headers, timeout=10)
            s.raise_for_status()
            feats = s.json().get('features', [])
            if feats:
                station_id = feats[0]['properties']['stationIdentifier']
                o = self.session.get(
                    f'https://api.weather.gov/stations/{station_id}/observations/latest',
                    headers=headers, timeout=10
                )
                o.raise_for_status()
                props = o.json().get('properties', {})
                t = (props.get('temperature') or {}).get('value')
                h = (props.get('relativeHumidity') or {}).get('value')
                w = (props.get('windSpeed') or {}).get('value')
                if t is not None:
                    temp_f = round(t * 9 / 5 + 32)
                if h is not None:
                    humidity_pct = round(h)
                if w is not None:
                    # NWS returns wind in km/h
                    wind_mph = round(w * 0.621371)
        except Exception as e:
            logger.warning(f'NWS observation fetch failed, using period 0 temp: {e}')
            if periods:
                temp_f = periods[0].get('temperature') or 0

        # Build Open-Meteo-shaped daily from day/night period pairs
        daily_time, daily_code, daily_max, daily_min = [], [], [], []
        i = 0
        while i < len(periods):
            per = periods[i]
            if per.get('isDaytime'):
                high = per.get('temperature')
                date = (per.get('startTime') or '')[:10]
                code = _nws_desc_to_code(per.get('shortForecast', ''))
                low = high
                if i + 1 < len(periods) and not periods[i + 1].get('isDaytime'):
                    low = periods[i + 1].get('temperature', high)
                    i += 2
                else:
                    i += 1
                daily_time.append(date)
                daily_code.append(code)
                daily_max.append(high)
                daily_min.append(low)
            else:
                # Leading night period — use for tonight's low against today
                i += 1

        return {
            'current': {
                'temperature_2m': temp_f,
                'relative_humidity_2m': humidity_pct,
                'wind_speed_10m': wind_mph,
                'weather_code': current_code,
            },
            'daily': {
                'time': daily_time,
                'weather_code': daily_code,
                'temperature_2m_max': daily_max,
                'temperature_2m_min': daily_min,
            },
        }

    def get_weather(self, lat, lon):
        """Fetch weather with provider fallback chain: Open-Meteo → NWS → disk cache."""
        cached = cache.get('weather')
        if cached:
            return cached

        providers = [
            ('open-meteo', self._fetch_open_meteo),
            ('nws', self._fetch_nws),
        ]
        for name, fn in providers:
            try:
                data = fn(lat, lon)
                if data:
                    cache.set('weather', data, CACHE_TTL_WEATHER)
                    _save_weather_cache(data)
                    logger.info(f'Weather served from {name}')
                    return data
            except Exception as e:
                logger.warning(f'Weather provider {name} failed: {e}')

        # Both live providers failed — fall back to stale on-disk copy
        stale = _load_weather_cache()
        if stale:
            logger.warning('All weather providers failed, serving stale disk cache')
            return stale
        logger.error('No weather available from any source')
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

    Uses ZONE_COMPLETED events only — duration is read directly from the
    summary string, e.g. "Backyard gndcover completed watering at 10:20 AM for 20 minutes."
    No start/complete pairing needed, so no bogus durations possible.
    """
    import re
    watering_events = []
    for ev in raw_events:
        if ev.get('type') != 'ZONE_STATUS' or ev.get('subType') != 'ZONE_COMPLETED':
            continue
        summary = ev.get('summary', '')
        if ' completed watering' not in summary:
            continue
        zone_name = summary.split(' completed watering')[0].strip()
        # Parse duration from summary — Rachio uses several formats:
        #   "for 45 minutes"
        #   "for 1 hour"
        #   "for 1 hour 30 minutes"
        hours_m = re.search(r'for (\d+) hour', summary)
        mins_m  = re.search(r'for (?:\d+ hour[s]? )?(\d+) minute', summary)
        hours = int(hours_m.group(1)) if hours_m else 0
        mins  = int(mins_m.group(1))  if mins_m  else 0
        duration_sec = hours * 3600 + mins * 60
        if not hours_m and not mins_m:
            logger.warning(f'Could not parse duration from summary: {summary!r}')
            continue
        if duration_sec < 300:  # skip runs under 5 minutes
            continue
        # Use completion time minus duration as approximate start time
        completion_ms = ev.get('eventDate', 0)
        start_ms = completion_ms - duration_sec * 1000
        watering_events.append({
            'zone_name': zone_name,
            'start_ms': start_ms,
            'duration_sec': duration_sec,
        })

    return watering_events

def compute_scheduled_runtime(schedule_rules, zone_id):
    """Compute scheduled daily and weekly runtime in minutes for a zone.

    Reads from schedule_rules config rather than historical events, so values
    reflect the intended schedule regardless of weather skips or pauses.
    Returns (daily_min, weekly_min) as rounded integers.

    Handles:
      INTERVAL_N  — runs every N days (e.g. INTERVAL_2 = every other day = 3.5×/wk)
      DAY_OF_WEEK_N — runs on specific days of the week (count = runs/wk)
    """
    weekly_sec = 0.0

    for rule in schedule_rules:
        if not rule.get('enabled', False):
            continue

        # Find this zone's per-run duration within this rule
        zone_duration_sec = None
        for z in rule.get('zones', []):
            if z.get('zoneId') == zone_id:
                zone_duration_sec = z.get('duration', 0)
                break
        if zone_duration_sec is None:
            continue  # zone not in this rule

        job_types = rule.get('scheduleJobTypes', [])

        # Interval-based: INTERVAL_N means "every N days"
        runs_per_week = None
        for jt in job_types:
            if jt.startswith('INTERVAL_'):
                try:
                    n = int(jt.split('_')[1])
                    runs_per_week = 7.0 / n
                except (IndexError, ValueError):
                    pass
                break

        # Day-of-week: count how many specific days are scheduled
        if runs_per_week is None:
            dow_count = sum(1 for jt in job_types if jt.startswith('DAY_OF_WEEK_'))
            if dow_count:
                runs_per_week = float(dow_count)

        if runs_per_week is None:
            logger.warning(f'Unknown schedule type for rule "{rule.get("name")}": {job_types}')
            continue

        weekly_sec += zone_duration_sec * runs_per_week

    weekly_min = round(weekly_sec / 60)
    daily_min  = round(weekly_sec / 7 / 60)
    return daily_min, weekly_min

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
    schedule_rules = device.get('scheduleRules', [])

    # Persist the durable skeleton (zone metadata + schedule rules) so the
    # dashboard can fall back to an offline view when the API budget is burned.
    try:
        _save_rachio_config(zones, schedule_rules, datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.warning(f'Failed to persist rachio config skeleton: {e}')
    # Note: events are not fetched here — runtimes are schedule-based, not event-based.
    # Use /api/debug/events for historical event inspection.

    zone_data = []
    all_schedule_info = []
    sensor_zone_map = config.get('sensor_zone_map', {})

    for zone in zones:
        zone_id = zone.get('id', '')
        zone_name = zone.get('name', f'Zone {zone.get("zoneNumber", "?")}')
        try:
            if not zone.get('enabled', True):
                continue

            image_url = zone.get('imageUrl', '') or config.get('zone_images', {}).get(zone_id, '')

            # Find linked sensors
            sensors_list = []
            zone_moisture = None

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
                    s_status, s_color = moisture_status(moisture_val)
                    sensors_list.append({
                        'id': eui[-4:],  # Last 4 chars as short ID
                        'eui': eui,
                        'moisture': round(moisture_val, 1),
                        'color': s_color,
                    })
                    if zone_moisture is None:
                        zone_moisture = moisture_val
                    else:
                        zone_moisture = (zone_moisture + moisture_val) / 2  # Average if multiple

            status, color = moisture_status(zone_moisture)

            # Compute scheduled runtimes from schedule_rules config — don't let a bad rule
            # take down the whole zone fetch.
            try:
                r1d, r7d = compute_scheduled_runtime(schedule_rules, zone_id)
            except Exception as e:
                logger.error(f'compute_scheduled_runtime failed for zone "{zone_name}" ({zone_id}): {e}', exc_info=True)
                r1d, r7d = None, None

            # Find schedule for this zone
            sched_name = None
            next_run_str = None
            for rule in schedule_rules:
                if not rule.get('enabled', False):
                    continue
                rule_zone_ids = [z.get('zoneId', z.get('id', '')) for z in rule.get('zones', [])]
                if zone_id in rule_zone_ids:
                    sched_name = rule.get('name')
                    try:
                        next_run_str = compute_next_run(rule)
                    except Exception as e:
                        logger.error(f'compute_next_run failed for rule "{sched_name}" on zone "{zone_name}": {e}', exc_info=True)
                        next_run_str = None
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
        except Exception as e:
            # One bad zone shouldn't take down the whole build. Log it loudly and skip.
            logger.error(f'build_zone_data: zone "{zone_name}" ({zone_id}) crashed: {e}', exc_info=True)
            _record_last_error(f'zone "{zone_name}" build failed: {e}')
            continue

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
            'lastSync': _last_sensecraft_sync,
        },
        'rachio': {
            'connected': rachio is not None and RACHIO_API_KEY != '',
            'deviceName': rachio.device_name if rachio else None,
            'zoneCount': len(zones),
            'apiRemaining': rachio.api_remaining if rachio else None,
            'tokensPerHour': rachio.tokens_per_hour() if rachio else None,
            'callsToday': rachio.calls_today if rachio else None,
            'lastError': _last_error,
        }
    }

def _next_rachio_reset_iso():
    """Return ISO timestamp for the next 5 PM Phoenix local (00:00 UTC) reset."""
    now = datetime.now(timezone.utc)
    next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_reset.isoformat()

# ── Main aggregation ──
dashboard_data = {}
data_lock = threading.Lock()
_last_good_zones = []           # preserve last successful zone fetch
_last_good_zones_at = None      # ISO timestamp of last good zone data
_last_error = None              # {'message': str, 'at': iso} — most recent aggregate/zone error, for UI surface

def _record_last_error(msg: str) -> None:
    """Remember the last thing that went wrong so we can show it on the dashboard."""
    global _last_error
    _last_error = {
        'message': str(msg)[:500],
        'at': datetime.now(timezone.utc).isoformat(),
    }
_ZONE_CACHE_FILE = Path(__file__).parent / '.zone_cache.json'
_last_good_telemetry: Dict[str, list] = {}  # eui → last successful telemetry payload
_last_sensecraft_sync: Optional[str] = None  # ISO timestamp of last successful SenseCraft telemetry fetch

def _save_zone_cache(zones, timestamp):
    """Persist last good zone data to disk so it survives restarts."""
    try:
        with open(_ZONE_CACHE_FILE, 'w') as f:
            json.dump({'zones': zones, 'cachedAt': timestamp}, f)
    except Exception as e:
        logger.warning(f'Failed to write zone cache: {e}')

def _load_zone_cache():
    """Load cached zone data from disk."""
    try:
        if _ZONE_CACHE_FILE.exists():
            with open(_ZONE_CACHE_FILE) as f:
                data = json.load(f)
            zones = data.get('zones', [])
            ts = data.get('cachedAt')
            if zones:
                logger.info(f'Loaded {len(zones)} zones from disk cache ({ts})')
                return zones, ts
    except Exception as e:
        logger.warning(f'Failed to read zone cache: {e}')
    return [], None

# ── Durable Rachio config cache (zone metadata that rarely changes) ──
# Unlike .zone_cache.json (fast-changing telemetry), this file holds the
# skeleton we can always fall back on when the API budget is burned:
# zone ids, names, numbers, image URLs, and schedule rules. Saved once per
# successful fetch; used to render the "offline skeleton" when live data
# is unavailable.
_RACHIO_CONFIG_FILE = Path(__file__).parent / '.rachio_config.json'

def _save_rachio_config(zones, schedule_rules, timestamp):
    """Persist durable zone metadata for offline-skeleton fallback."""
    try:
        skeleton = [
            {
                'id': z.get('id', ''),
                'name': z.get('name', ''),
                'zoneNumber': z.get('zoneNumber', 0),
                'enabled': z.get('enabled', True),
                'imageUrl': z.get('imageUrl', ''),
            }
            for z in zones
        ]
        with open(_RACHIO_CONFIG_FILE, 'w') as f:
            json.dump({
                'zones': skeleton,
                'scheduleRules': schedule_rules or [],
                'cachedAt': timestamp,
            }, f, indent=2)
    except Exception as e:
        logger.warning(f'Failed to write rachio config cache: {e}')

def _load_rachio_config():
    """Load durable zone metadata for offline-skeleton fallback."""
    try:
        if _RACHIO_CONFIG_FILE.exists():
            with open(_RACHIO_CONFIG_FILE) as f:
                data = json.load(f)
            if data.get('zones'):
                return data
    except Exception as e:
        logger.warning(f'Failed to read rachio config cache: {e}')
    # Bootstrap from config.json `_comments` so the skeleton works the very
    # first time Rachio is unreachable, before any successful fetch has
    # populated the cache file.
    return _bootstrap_rachio_config_from_comments()

def _bootstrap_rachio_config_from_comments():
    """Parse config.json `_comments` to derive a minimal zone list.

    Comment format is expected to be e.g. "orange tree → Citrus trees (zone 9)".
    Returns None if no usable metadata can be extracted.
    """
    import re
    cfg = config or {}
    comments = cfg.get('_comments', {}) or {}
    sensor_map = cfg.get('sensor_zone_map', {}) or {}
    if not comments or not sensor_map:
        return None

    # Collect unique (zone_id, name, number) triples from comments
    seen = {}  # zone_id -> (name, number)
    for eui, zone_id in sensor_map.items():
        comment = comments.get(eui, '')
        if not comment or not zone_id:
            continue
        m = re.search(r'→\s*(.+?)\s*\(zone\s+(\d+)\)', comment)
        if not m:
            continue
        name = m.group(1).strip()
        try:
            number = int(m.group(2))
        except ValueError:
            number = 0
        # Prefer the first mapping we see; repeats (multiple sensors per zone)
        # should agree anyway.
        seen.setdefault(zone_id, (name, number))

    if not seen:
        return None

    zones = [
        {'id': zid, 'name': name, 'zoneNumber': number, 'enabled': True, 'imageUrl': ''}
        for zid, (name, number) in seen.items()
    ]
    zones.sort(key=lambda z: z['zoneNumber'])
    logger.info(f'Bootstrapped rachio config skeleton from config.json comments ({len(zones)} zones)')
    return {'zones': zones, 'scheduleRules': [], 'cachedAt': None, 'bootstrap': True}

def build_skeleton_zones(sensecraft):
    """Build a minimal offline zone list from durable config + live SenseCraft moisture.

    Used when the Rachio budget is exhausted and we have no live zone cache.
    Runtimes are None (rendered as '??') and the 'offline' flag marks tiles so
    the UI can signal degraded state.
    """
    cfg = _load_rachio_config()
    if not cfg:
        return [], []

    raw_zones = cfg.get('zones', [])
    schedule_rules = cfg.get('scheduleRules', []) or []
    sensor_zone_map = config.get('sensor_zone_map', {})

    out = []
    for z in raw_zones:
        if not z.get('enabled', True):
            continue
        zone_id = z.get('id', '')
        zone_name = z.get('name', f'Zone {z.get("zoneNumber", "?")}')
        image_url = z.get('imageUrl', '') or config.get('zone_images', {}).get(zone_id, '')

        # Live moisture from SenseCraft (works independently of Rachio)
        sensors_list = []
        zone_moisture = None
        for eui, mapped_zone_id in sensor_zone_map.items():
            if mapped_zone_id != zone_id or not sensecraft:
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
                    'id': eui[-4:],
                    'eui': eui,
                    'moisture': round(moisture_val, 1),
                    'color': color,
                })
                zone_moisture = moisture_val if zone_moisture is None else (zone_moisture + moisture_val) / 2

        status, color = moisture_status(zone_moisture)

        # Try to compute scheduled runtime from cached schedule rules —
        # this still works offline because schedules are durable config.
        try:
            r1d, r7d = compute_scheduled_runtime(schedule_rules, zone_id)
        except Exception:
            r1d, r7d = None, None

        out.append({
            'name': zone_name,
            'id': zone_id,
            'zoneNumber': z.get('zoneNumber', 0),
            'imageUrl': image_url,
            'moisture': round(zone_moisture, 1) if zone_moisture is not None else None,
            'status': status,
            'statusColor': color,
            'sensors': sensors_list,
            'runtime1d': r1d,
            'runtime7d': r7d,
            'nextRun': None,
            'scheduleName': None,
            'offline': True,   # UI badge: this tile is running on skeleton data
        })

    return out, []

# Restore cache from disk on startup
_last_good_zones, _last_good_zones_at = _load_zone_cache()

def aggregate_all_data():
    global _last_good_zones, _last_good_zones_at, _last_sensecraft_sync
    logger.info('Aggregating dashboard data...')
    try:
        rachio = RachioAPI(RACHIO_API_KEY) if RACHIO_API_KEY else None
        sensecraft = SenseCraftAPI(SENSECRAFT_API_KEY, SENSECRAFT_API_SECRET) if SENSECRAFT_API_KEY and SENSECRAFT_API_SECRET else None
        weather_api = WeatherAPI()

        zones = []
        schedule_info = []
        device_id = None

        # Distinguish two reasons we might skip the Rachio fetch:
        #   attempted_fetch=True  → we tried, and zones tells us whether it worked.
        #   attempted_fetch=False → we deliberately didn't try (refresh floor, rate-limit, cap).
        #                            In this case cached zones should be considered FRESH, not stale,
        #                            because we're respecting our own refresh interval on purpose.
        attempted_fetch = False
        if rachio:
            # Arm the crash-loop floor once for the whole cycle so sequential calls
            # (person_id → device_ids → device) don't block each other. If we can't
            # even begin a cycle (crash-loop floor active, rate-limited, or daily cap),
            # skip Rachio entirely and fall through to cache/skeleton.
            if rachio.can_begin_cycle:
                attempted_fetch = True
                rachio.begin_cycle()
                device_ids = rachio.get_device_ids()
                if device_ids:
                    device_id = device_ids[0]
                    zones, schedule_info = build_zone_data(rachio, sensecraft, device_id)
            else:
                logger.info(f'Rachio cycle skipped ({rachio._block_reason()}); using cache')
                # Try to pick up a device_id from hydrated state so downstream code can still
                # reference it (e.g. for event fetches from cache).
                if rachio._device_ids_cached:
                    device_id = rachio._device_ids_cached[0]

        weather_raw = weather_api.get_weather(LATITUDE, LONGITUDE)
        now = datetime.now(timezone.utc).isoformat()

        # Fallback ladder when live Rachio data is unavailable:
        # 1. Fresh fetch succeeded → use it.
        # 2. Recent telemetry cache exists → serve stale with banner.
        # 3. Durable skeleton exists → build offline tiles from config + live SenseCraft moisture.
        # 4. Nothing → empty grid with helpful alert.
        offline = False
        if zones:
            _last_good_zones = zones
            _last_good_zones_at = now
            _save_zone_cache(zones, now)
            stale = False
            # Fresh Rachio data is good for hours — push the circuit-breaker floor out
            # to REFRESH_INTERVAL so we stop hitting the API until the next scheduled cycle.
            if rachio:
                rachio._schedule_next_call(REFRESH_INTERVAL)
        elif _last_good_zones:
            zones = _last_good_zones
            # Cached zones are only STALE if we actually tried to refresh and failed.
            # If we deliberately skipped because we're inside our own refresh interval,
            # the cache is still the current canonical view — not stale.
            if attempted_fetch:
                stale = True
                logger.warning(f'Fetch failed — using cached zone data from {_last_good_zones_at}')
            else:
                stale = False
        else:
            skeleton_zones, _ = build_skeleton_zones(sensecraft)
            if skeleton_zones:
                zones = skeleton_zones
                offline = True
                stale = True
                logger.warning(f'Serving {len(zones)} offline skeleton zones (no telemetry cache)')
            else:
                stale = False

        # If zones have moisture data but we haven't recorded a sync time yet
        # (e.g. using cached zones after restart), seed the timestamp now.
        if not _last_sensecraft_sync and any(z.get('moisture') is not None for z in zones):
            _last_sensecraft_sync = now

        services = build_services(rachio, sensecraft, zones)
        services['rachio']['resetAt'] = _next_rachio_reset_iso()
        services['rachio']['offline'] = offline

        return {
            'zones': zones,
            'alerts': build_alerts(zones, schedule_info),
            'weather': build_weather(weather_raw),
            'services': services,
            'updatedAt': now,
            'stale': stale,
            'offline': offline,
            'staleDataFrom': _last_good_zones_at if stale else None,
        }
    except Exception as e:
        logger.error(f'Aggregation failed: {e}', exc_info=True)
        _record_last_error(f'aggregate failed: {e}')
        now = datetime.now(timezone.utc).isoformat()
        # Even on total failure, preserve cached zones
        zones = _last_good_zones if _last_good_zones else []
        stale = bool(_last_good_zones)
        return {
            'zones': zones,
            'alerts': [{'level': 'crit', 'icon': '❌', 'message': f'Dashboard error: {e}'}],
            'weather': None,
            'services': {'rachio': {'lastError': _last_error}},
            'updatedAt': now,
            'stale': stale,
            'staleDataFrom': _last_good_zones_at if stale else None,
        }

# ── Background refresh thread ──
RETRY_INTERVAL_NO_DATA = 300   # 5 min — no cached data at all, need something to show
RETRY_INTERVAL_STALE = 900     # 15 min — have cached data, back off to avoid burning tokens

def background_refresh():
    global dashboard_data
    while True:
        with data_lock:
            is_stale = dashboard_data.get('stale', False)
            has_zones = bool(dashboard_data.get('zones'))

        if not has_zones:
            sleep_time = RETRY_INTERVAL_NO_DATA
            logger.info(f'No zone data at all — retrying in {RETRY_INTERVAL_NO_DATA}s')
        elif is_stale:
            sleep_time = RETRY_INTERVAL_STALE
            logger.info(f'Using cached zones — retrying in {RETRY_INTERVAL_STALE}s')
        else:
            sleep_time = REFRESH_INTERVAL
        time.sleep(sleep_time)
        try:
            new_data = aggregate_all_data()
            with data_lock:
                dashboard_data = new_data
            if new_data.get('zones') and not new_data.get('stale'):
                logger.info(f'Background refresh complete — {len(new_data["zones"])} zones (fresh)')
            elif new_data.get('zones'):
                logger.warning(f'Background refresh — {len(new_data["zones"])} zones (stale cache)')
            else:
                logger.warning('Background refresh complete — no zones at all')
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

@app.route('/favicon.ico')
def favicon():
    """Return empty 204 so browsers stop logging 404s."""
    return ('', 204)

@app.route('/api/data')
def api_data():
    with data_lock:
        return jsonify(dashboard_data)

@app.route('/api/force-refresh', methods=['POST'])
def api_force_refresh():
    """Force an immediate data refresh, bypassing the background timer.

    Respects the Rachio circuit-breaker (rate limits, daily cap) but will
    always re-fetch SenseCraft moisture data.
    """
    try:
        # Bust the SenseCraft cache so we actually re-fetch moisture from the API
        cache.clear_prefix('sensecraft_')
        new_data = aggregate_all_data()
        with data_lock:
            global dashboard_data
            dashboard_data = new_data
        return jsonify({'ok': True, 'updatedAt': new_data.get('updatedAt')})
    except Exception as e:
        logger.error(f'Force refresh failed: {e}', exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500

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
    device = rachio.get_device(device_ids[0])
    raw = rachio.get_events(device_ids[0]) or []
    events = parse_watering_events(raw)
    zone_names_in_events = sorted(set(e['zone_name'] for e in events))
    zone_names_in_data = [z['name'] for z in zones]
    raw_zone_events = [e for e in raw if e.get('type') == 'ZONE_STATUS'][:20]
    schedule_rules = device.get('scheduleRules', []) if device else []
    return jsonify({
        'zone_names_in_events': zone_names_in_events,
        'zone_names_in_data': zone_names_in_data,
        'watering_events': events[:50],
        'raw_zone_events': raw_zone_events,
        'schedule_rules': schedule_rules,
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
