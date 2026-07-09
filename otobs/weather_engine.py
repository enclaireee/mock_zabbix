"""Deterministic weather generator for the Omega-Realistic sim mode.

Every value is a pure function of a Unix timestamp — no random.* calls, no
mutable state — so `make backfill` produces byte-identical weather for the
same historical window on every re-run, and the live simulator and the
backfill sweep agree on "what the weather was" at any given instant.
Tuned for a tropical (South Sumatra / West Java) climate: two seasons, not
four, and fast convective-afternoon rain rather than frontal systems.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from functools import lru_cache

DAYS_PER_YEAR = 365.25

_DRY_PEAK_DOY = 227.0    # dry-season peak ~mid-August; wet peak is exactly half a year away

_TEMP_MIN_HOUR = 5.0     # coldest just before sunrise
_TEMP_MAX_HOUR = 14.0    # hottest ~2h after solar noon (thermal lag)

_RAIN_PEAK_HOUR = 17.0        # tropical convective afternoon storms
_RAIN_HALF_WIDTH_HOURS = 2.0  # window ~15:00-19:00, per the brief's own example
_RAIN_MAX_MM_HR = 45.0

_LIGHTNING_RAIN_THRESHOLD = 8.0  # mm/hr — lighter rain doesn't strike
_LIGHTNING_BUCKET_S = 60         # re-roll the deterministic gate once a minute
_LIGHTNING_STRIKE_PCT = 35       # % chance per bucket once the threshold is crossed

_DUST_RESET_RAIN_MM_HR = 2.0     # any rain above this washes accumulated dust out
_DUST_HUMIDITY_FLOOR = 65.0      # dust only accumulates below this RH
_DUST_PER_DRY_HOUR = 3.0
_DUST_MAX = 100.0
_DUST_LOOKBACK_HOURS = 168       # 7d bounded backward search for the last reset —
                                 # a 48h cap left "failed" unreachable in practice
                                 # (only ~14 dry hours accumulate per 2-day window)


class WeatherNode:
    """Stateless — get_weather depends only on its timestamp argument, so
    concurrent/out-of-order calls (live-loop worker threads, backfill's
    historical sweep) always agree for the same instant."""

    def get_weather(self, timestamp: float) -> dict:
        return _weather_at(float(timestamp))


@lru_cache(maxsize=8)
def _weather_at(timestamp: float) -> dict:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    doy = dt.timetuple().tm_yday + dt.hour / 24.0
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0

    season = _seasonal_factor(doy)  # +1 at the dry peak, -1 at the wet peak
    temp = _diurnal_temp(hour, season)
    rain = _rain_intensity(hour, season)

    return {
        "temp": round(temp, 2),
        "humidity": round(_humidity(temp), 2),
        "rain_intensity": round(rain, 2),
        "lightning_event": _lightning_event(timestamp, rain),
        "dust_index": round(_dust_index(timestamp), 2),
    }


def _seasonal_factor(doy: float) -> float:
    """+1 at the dry-season peak (~mid-Aug), -1 at the wet-season peak
    (~mid-Feb), smooth cosine in between — the standard first-order model
    for a tropical two-season climate."""
    return math.cos(2 * math.pi * (doy - _DRY_PEAK_DOY) / DAYS_PER_YEAR)


def _diurnal_temp(hour: float, season: float) -> float:
    """Asymmetric double-cosine: a fast ~9h rise from the 05:00 minimum to the
    14:00 maximum, a slower ~15h cool back down — smooth (C1-continuous) at
    both anchor hours. Base range fluctuates 25-35 C with the season.
    ponytail: a lightweight stand-in for the full Parton & Logan sunrise/
    sunset solar-geometry model, which needs latitude + day-length inputs
    this mock generator doesn't carry. Swap in real solar geometry if a
    specific site's day-length ever needs to matter."""
    day_range = 6.0 + 2.0 * season         # widens toward the hot dry season
    t_min = max(25.0, min(35.0, 27.0 - 1.5 * season))
    t_max = max(25.0, min(35.0, t_min + day_range))

    if _TEMP_MIN_HOUR <= hour <= _TEMP_MAX_HOUR:
        frac = (hour - _TEMP_MIN_HOUR) / (_TEMP_MAX_HOUR - _TEMP_MIN_HOUR)
        return t_min + (t_max - t_min) * (1 - math.cos(math.pi * frac)) / 2
    span = 24.0 - (_TEMP_MAX_HOUR - _TEMP_MIN_HOUR)
    frac = ((hour - _TEMP_MAX_HOUR) % 24.0) / span
    return t_max + (t_min - t_max) * (1 - math.cos(math.pi * frac)) / 2


def _humidity(temp: float) -> float:
    """Deterministic inverse of temp: absolute humidity is ~stable through the
    day, so RH falls as temp rises and recovers as it cools. Range 50-95%."""
    temp_frac = (temp - 25.0) / (35.0 - 25.0)
    return max(50.0, min(95.0, 95.0 - 45.0 * temp_frac))


def _rain_intensity(hour: float, season: float) -> float:
    """Convective afternoon rain, gated by season: a bell-shaped window around
    _RAIN_PEAK_HOUR, scaled by how deep into the wet season `season` is."""
    wet = max(0.0, -season)  # 0 in the dry season, up to 1 at the wet peak
    if wet <= 0.0:
        return 0.0
    delta = min(abs(hour - _RAIN_PEAK_HOUR), 24.0 - abs(hour - _RAIN_PEAK_HOUR))
    window = max(0.0, math.cos(math.pi * delta / (2 * _RAIN_HALF_WIDTH_HOURS)))
    return wet * window * _RAIN_MAX_MM_HR


def _lightning_event(timestamp: float, rain: float) -> int:
    """Deterministic pulse: only possible once rain crosses the storm
    threshold, gated per-minute by a pure arithmetic hash of the timestamp —
    NOT Python's built-in hash() on a str/object, which is randomly salted
    per-process and would make the same historical minute strike on one run
    and not the next."""
    if rain < _LIGHTNING_RAIN_THRESHOLD:
        return 0
    bucket = int(timestamp) // _LIGHTNING_BUCKET_S
    scrambled = (bucket * 2654435761) % (2 ** 32)  # Knuth multiplicative hash
    return 1 if (scrambled % 100) < _LIGHTNING_STRIKE_PCT else 0


def _dust_index(timestamp: float) -> float:
    """A true running integral, but re-derived on demand from the deterministic
    rain/temp curves rather than kept as mutable instance state — state would
    make backfill's result depend on call order/frequency instead of purely
    on the timestamp, which is exactly what determinism forbids. Walks back
    hour by hour to the last rain-reset, sums the dry hours since.
    ponytail: bounded 7d lookback + a hard cap, not an unbounded integral —
    only the diurnally-dry hours (not every hour) count, so reaching the top
    of the range needs most of a week; cheap enough for a mock generator
    (worst case ~168 iterations of plain arithmetic per call). A persisted
    last-reset timestamp is the upgrade path if that cost ever shows up in
    a backfill profile."""
    dry_hours = 0
    for step in range(_DUST_LOOKBACK_HOURS):
        t = timestamp - step * 3600.0
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        doy = dt.timetuple().tm_yday + dt.hour / 24.0
        hour = dt.hour + dt.minute / 60.0
        season = _seasonal_factor(doy)
        if _rain_intensity(hour, season) > _DUST_RESET_RAIN_MM_HR:
            break
        if _humidity(_diurnal_temp(hour, season)) < _DUST_HUMIDITY_FLOOR:
            dry_hours += 1
    return min(_DUST_MAX, dry_hours * _DUST_PER_DRY_HOUR)
