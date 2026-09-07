#!/usr/bin/env python3
"""
Perfil fisico del deportista (DISENO_ANALISIS_TRAMOS seccion 8), guardado en
data/profile.json:

- resting_hr: pulso en reposo real. NO se puede inferir de forma fiable de
  las actividades (el minimo visto en una salida ya esta muy por encima del
  reposo real), asi que se pide directamente al usuario. El pulso maximo, en
  cambio, si se infiere del historico de actividades (ver trimp.py), no hace
  falta guardarlo aqui.
- height_cm: altura, valor fijo (no cambia). Normaliza la longitud de
  zancada en running (85 cm no es lo mismo en 1,90 m que en 1,60 m).
- weight_history: el peso SI puede cambiar, asi que se guarda como historial
  con fecha (como las actividades) en vez de un unico valor -- necesario
  para vatios/kg en cada tramo con el peso que tocaba en esa fecha, no el
  peso de hoy.
"""

import json
import os
import threading
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
PROFILE_FILE = DATA_DIR / "profile.json"

# El servidor atiende cada peticion HTTP en su propio hilo (ThreadingHTTPServer
# en app.py) -- sin este candado, dos guardados casi simultaneos (p.ej. altura
# y peso seguidos desde Ajustes) pueden escribir profile.json a la vez y
# corromperlo (los dos truncan el fichero con 'w' y se entrelazan sus
# escrituras). Protege el ciclo leer-modificar-escribir entero, no solo la
# escritura, para que tampoco se pierda un cambio por una condicion de carrera
# de "el que escribe el ultimo gana" sobre una lectura ya obsoleta.
_lock = threading.Lock()

DEFAULTS = {
    "resting_hr": None,
    "sex": "M",
    "height_cm": None,
    "weight_history": [],  # [{"date": "YYYY-MM-DD", "weight_kg": 78.0}, ...]
}


def load_profile():
    profile = dict(DEFAULTS)
    if PROFILE_FILE.exists():
        try:
            with open(PROFILE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULTS:
                if k in saved:
                    profile[k] = saved[k]
        except Exception:
            pass
    return profile


def _save(profile):
    """Escritura atomica: a un fichero temporal y luego os.replace() (que en
    Windows y Linux reemplaza en una sola operacion de sistema), para que un
    lector concurrente (o un corte de luz a medias) nunca vea un JSON a
    medio escribir -- solo el fichero viejo completo o el nuevo completo."""
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = PROFILE_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, PROFILE_FILE)
    return profile


def save_profile(partial):
    with _lock:
        merged = load_profile()
        if "resting_hr" in partial:
            v = partial["resting_hr"]
            merged["resting_hr"] = float(v) if v not in (None, "") else None
        if "sex" in partial and partial["sex"] in ("M", "F"):
            merged["sex"] = partial["sex"]
        if "height_cm" in partial:
            v = partial["height_cm"]
            merged["height_cm"] = float(v) if v not in (None, "") else None
        return _save(merged)


def add_weight_entry(iso_date, weight_kg):
    """Añade (o sobreescribe, si ya hay una pesada ese mismo dia) una entrada
    del historial de peso, y lo deja ordenado por fecha."""
    with _lock:
        profile = load_profile()
        history = [e for e in profile["weight_history"] if e["date"] != iso_date]
        history.append({"date": iso_date, "weight_kg": float(weight_kg)})
        history.sort(key=lambda e: e["date"])
        profile["weight_history"] = history
        return _save(profile)


def delete_weight_entry(iso_date):
    with _lock:
        profile = load_profile()
        profile["weight_history"] = [e for e in profile["weight_history"] if e["date"] != iso_date]
        return _save(profile)


def weight_at_date(iso_date, profile=None):
    """Peso vigente en una fecha dada: la entrada mas reciente EN o ANTES de
    esa fecha; si todas las entradas son posteriores (p.ej. actividad mas
    antigua que la primera pesada registrada), se usa la mas antigua
    disponible como mejor aproximacion. None si no hay ningun dato de peso."""
    profile = profile or load_profile()
    history = profile.get("weight_history") or []
    if not history:
        return None
    before = [e for e in history if e["date"] <= iso_date]
    if before:
        return before[-1]["weight_kg"]
    return history[0]["weight_kg"]


def latest_weight(profile=None):
    profile = profile or load_profile()
    history = profile.get("weight_history") or []
    return history[-1]["weight_kg"] if history else None


def today_iso():
    return date.today().isoformat()
