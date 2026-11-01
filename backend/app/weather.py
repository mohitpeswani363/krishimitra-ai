"""Live weather and soil conditions from Open-Meteo (no API key needed).

Resilience: every call retries with backoff on network errors, 429 and 5xx. Successful forecasts are cached
for FRESH_SECONDS; if the service is down afterwards, the last real forecast is served (up to STALE_SECONDS old)
and explicitly marked stale with its age. Nothing is ever simulated: with no real forecast available the
caller gets WeatherUnavailable.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FRESH_SECONDS = 15 * 60
STALE_SECONDS = 6 * 60 * 60
RETRY_DELAYS = (0.5, 1.5)

_geo_cache: dict[tuple[str, str], tuple[float, float, str]] = {}
_forecast_cache: dict[tuple[float, float], tuple[float, dict]] = {}


class WeatherUnavailable(Exception):
    pass


class LocationNotFound(Exception):
    pass


async def get_json(url: str, params: dict, timeout: float = 8, retries: bool = True) -> dict:
    """GET with retries for transient failures (network errors, 429, 5xx)."""
    last: Exception | None = None
    delays = RETRY_DELAYS if retries else ()
    for attempt in range(len(delays) + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                break  # client error: retrying will not help
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    raise last or RuntimeError("request failed")


async def geocode_district(district: str, state: str = "Bihar") -> tuple[float, float, str]:
    key = (district.strip().lower(), state.strip().lower())
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        data = await get_json(GEOCODE_URL, {"name": district.strip(), "count": 10, "language": "en", "format": "json"})
    except Exception as exc:
        raise WeatherUnavailable(f"Location service unreachable ({type(exc).__name__})") from exc
    results = [x for x in data.get("results", []) if x.get("country_code") == "IN"]
    in_state = [x for x in results if (x.get("admin1") or "").lower() == state.lower()]
    best = (in_state or results or [None])[0]
    if not best:
        raise LocationNotFound(f"Could not find district '{district}' in India. Check the spelling (e.g. Patna, Gaya, Muzaffarpur).")
    found = (best["latitude"], best["longitude"], best.get("name", district))
    _geo_cache[key] = found
    return found


def _start_index(times: list[str], now_iso: str | None) -> int:
    """Index of the current hour in Open-Meteo's hourly arrays (which start at local midnight)."""
    if not times or not now_iso:
        return 0
    hour = now_iso[:13]
    for i, t in enumerate(times):
        if t[:13] >= hour:
            return i
    return 0


def _avg(values: list) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else None


def parse_openmeteo(raw: dict) -> dict:
    current = raw["current"]
    hourly = raw.get("hourly", {})
    daily = raw.get("daily", {})
    i = _start_index(hourly.get("time", []), current.get("time"))
    window = slice(i, i + 24)
    at = lambda k: (hourly.get(k) or [None] * (i + 1))[i] if len(hourly.get(k) or []) > i else None
    topsoil = _avg([at("soil_moisture_0_to_1cm"), at("soil_moisture_1_to_3cm"), at("soil_moisture_3_to_9cm")])
    days = [
        {"date": d, "t_max": daily.get("temperature_2m_max", [None] * 9)[n], "t_min": daily.get("temperature_2m_min", [None] * 9)[n],
         "rain_mm": daily.get("precipitation_sum", [None] * 9)[n], "rain_prob_pct": daily.get("precipitation_probability_max", [None] * 9)[n]}
        for n, d in enumerate(daily.get("time", []))
    ]
    return {
        "mode": "live",
        "label": "Live Open-Meteo forecast",
        "temperature_c": current["temperature_2m"],
        "humidity_pct": current["relative_humidity_2m"],
        "wind_kmh": current["wind_speed_10m"],
        "rainfall_mm_next_24h": round(sum(v for v in (hourly.get("precipitation") or [])[window] if v), 1),
        "precipitation_probability_pct": max([v for v in (hourly.get("precipitation_probability") or [])[window] if v is not None] or [0]),
        "forecast_time": current["time"],
        "soil": {
            "moisture_topsoil_m3m3": topsoil,
            "moisture_rootzone_m3m3": at("soil_moisture_9_to_27cm"),
            "temperature_6cm_c": at("soil_temperature_6cm"),
            "source": "Open-Meteo soil model (0-9 cm and 9-27 cm volumetric water content)",
        },
        "daily": days,
        "source": "Open-Meteo",
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
    }


async def live_weather(lat: float, lon: float) -> dict:
    key = (round(lat, 2), round(lon, 2))
    cached = _forecast_cache.get(key)
    if cached and time.time() - cached[0] < FRESH_SECONDS:
        return dict(cached[1], cache="fresh")
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
        "hourly": "precipitation_probability,precipitation,soil_moisture_0_to_1cm,soil_moisture_1_to_3cm,"
                  "soil_moisture_3_to_9cm,soil_moisture_9_to_27cm,soil_temperature_6cm",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
    }
    try:
        data = parse_openmeteo(await get_json(FORECAST_URL, params))
    except Exception as exc:
        if cached and time.time() - cached[0] < STALE_SECONDS:
            age_min = int((time.time() - cached[0]) / 60)
            return dict(cached[1], cache="stale", stale_minutes=age_min,
                        label=f"Last real Open-Meteo forecast ({age_min} min old; service currently unreachable)")
        raise WeatherUnavailable(f"Open-Meteo forecast unreachable ({type(exc).__name__})") from exc
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _forecast_cache[key] = (time.time(), data)
    return dict(data, cache="miss")


async def weather_for_district(district: str, state: str) -> dict:
    lat, lon, name = await geocode_district(district, state)
    weather = await live_weather(lat, lon)
    weather.update({"location": f"{name}, {state}", "latitude": lat, "longitude": lon})
    return weather
