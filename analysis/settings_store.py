#!/usr/bin/env python3
"""
Los 3 settings configurables introducidos por DISENO_ANALISIS_TRAMOS (seccion 4):
divergencia maxima de pendiente y distancia maxima de tramo (usados por el
segmentador de interesting_points.py) y la ventana de dias del baseline
adaptativo (usada por baselines.py). Se guardan en data/settings.json y se
exponen en la interfaz (pantalla de Ajustes).
"""

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS = {
    "max_grade_divergence_pct": 3.0,
    "max_tramo_distance_m": 2000.0,
    "baseline_window_days": 90,
}


def load_settings():
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULTS:
                if k in saved:
                    settings[k] = saved[k]
        except Exception:
            pass
    return settings


def save_settings(partial):
    """Fusiona `partial` (solo las claves reconocidas) sobre los settings ya
    guardados y los persiste. Devuelve el resultado completo."""
    merged = load_settings()
    for k in DEFAULTS:
        if k in partial:
            merged[k] = float(partial[k])
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # escritura atomica (temp + os.replace): un lector concurrente nunca ve
    # un JSON a medio escribir -- ver el mismo patron, con el porque, en
    # profile_store.py.
    tmp_path = SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, SETTINGS_FILE)
    return merged
