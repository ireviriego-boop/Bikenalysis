#!/usr/bin/env python3
"""
Viento (velocidad + direccion) y temperatura en el punto/hora de inicio de
una actividad, a modo informativo -- el usuario pidio saber si hacia viento
(sobre todo en bici), pero ni Hammerhead ni Strava dan meteo en su API.

Se pide a Open-Meteo (gratis, sin clave para uso no comercial):
  - archive-api.open-meteo.com/v1/archive: reanalisis ERA5, cubre bien
    cualquier fecha de hace mas de unos pocos dias.
  - api.open-meteo.com/v1/forecast (con past_days): para actividades muy
    recientes (hoy/ayer/anteayer) que el archivo historico todavia no tiene.

Es un unico snapshot (la hora mas cercana al inicio de la actividad, en el
punto de partida), no un valor por tramo -- el viento puede cambiar durante
una salida larga, esto es una foto aproximada de las condiciones, no un
calculo de viento a favor/en contra por tramo (eso haria falta cruzarlo con
el rumbo de cada punto, que es bastante mas trabajo del pedido).

Se cachea en disco por actividad (data/weather_cache.json): una actividad ya
sincronizada no cambia de fecha ni de sitio, no hace falta volver a pedirlo
cada vez que se abre.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
CACHE_FILE = DATA_DIR / "weather_cache.json"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = "wind_speed_10m,wind_direction_10m,temperature_2m"

# El archivo ERA5 no tiene reanalisis todavia de los ultimos dias -- por
# debajo de este margen se usa la API de prevision con past_days en su lugar.
ARCHIVE_DELAY_DAYS = 5

COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]


def _load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)


def _fetch_json(url, params, timeout=8):
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "Bikenalysis/1.0 (uso personal)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _nearest_hour_value(hourly, target_dt):
    times = hourly.get("time") or []
    if not times:
        return None
    target_str = target_dt.strftime("%Y-%m-%dT%H:00")
    idx = times.index(target_str) if target_str in times else None
    if idx is None:
        best_diff = None
        for i, t in enumerate(times):
            try:
                dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            diff = abs((dt - target_dt.replace(tzinfo=None)).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff, idx = diff, i
    if idx is None:
        return None
    wind_speeds = hourly.get("wind_speed_10m") or []
    if idx >= len(wind_speeds) or wind_speeds[idx] is None:
        return None
    wind_dirs = hourly.get("wind_direction_10m") or []
    temps = hourly.get("temperature_2m") or []
    return {
        "wind_speed_kmh": wind_speeds[idx],
        "wind_direction_deg": wind_dirs[idx] if idx < len(wind_dirs) else None,
        "temperature_c": temps[idx] if idx < len(temps) else None,
    }


def wind_compass_label(deg):
    if deg is None:
        return None
    return COMPASS[round(deg / 45) % 8]


def fetch_weather_for_activity(activity_id, lat, lon, ts):
    """Viento/temperatura en el punto de inicio de la actividad, en la hora
    disponible mas cercana. None si no se ha podido conseguir el dato (sin
    red, servicio caido, etc.) -- nunca inventa un valor."""
    cache = _load_cache()
    if activity_id in cache:
        return cache[activity_id]

    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    date_str = dt_utc.strftime("%Y-%m-%d")
    days_ago = (datetime.now(timezone.utc).date() - dt_utc.date()).days

    result = None
    try:
        if days_ago > ARCHIVE_DELAY_DAYS:
            data = _fetch_json(ARCHIVE_URL, {
                "latitude": round(lat, 3), "longitude": round(lon, 3),
                "start_date": date_str, "end_date": date_str,
                "hourly": HOURLY_VARS, "wind_speed_unit": "kmh", "timezone": "UTC",
            })
            result = _nearest_hour_value(data.get("hourly") or {}, dt_utc)
        if result is None and 0 <= days_ago <= 92:
            data = _fetch_json(FORECAST_URL, {
                "latitude": round(lat, 3), "longitude": round(lon, 3),
                "hourly": HOURLY_VARS, "wind_speed_unit": "kmh", "timezone": "UTC",
                "past_days": max(1, days_ago + 1), "forecast_days": 1,
            })
            result = _nearest_hour_value(data.get("hourly") or {}, dt_utc)
    except Exception:
        result = None  # sin red, timeout, servicio caido... mejor no mostrar nada que un dato a medias

    cache[activity_id] = result
    _save_cache(cache)
    return result
