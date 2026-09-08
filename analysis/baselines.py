#!/usr/bin/env python3
"""
Calcula percentiles personales (no genericos) para calibrar los umbrales del
motor de tramos interesantes a la forma de rodar/correr del usuario en vez de
valores fijos inventados.

Separado por deporte (DISENO_ANALISIS_TRAMOS seccion 6): mezclar cadencia de
ciclismo (rpm, ~60-90) con cadencia de running (spm, ~170-190) en un mismo
baseline daria percentiles sin sentido para los dos. baselines.json queda con
dos arboles independientes, "cycling" y "running", con la misma forma cada
uno (hr/cadence/grade/speed/etc.) -- interesting_points.py elige el arbol
segun el deporte de cada actividad al clasificar sus tramos.

Baseline adaptativo por ventana movil (DISENO_ANALISIS_TRAMOS seccion 3): en
vez de usar todo el historico por igual, los percentiles se calculan sobre
las salidas de los ultimos `baseline_window_days` (configurable en Ajustes,
ver settings_store.py) para que reflejen la forma actual del usuario, no la
de hace meses. Si la ventana no tiene datos suficientes todavia (usuario
nuevo, o pocas salidas recientes), cada metrica cae de vuelta al historico
completo -- el respaldo se calcula por metrica, no por ride entero, para
aprovechar toda la ventana que SI haya en cada una.

Se calculan por separado:
  - hr:        pulsaciones (bpm), sobre todos los puntos con HR.
  - cadence:   rpm (ciclismo) o spm (running) sobre puntos con cadence > 0.
  - grade_up:  pendiente (%) solo de tramos de subida (grade > 0.5%).
  - grade_down:pendiente (%) solo de tramos de bajada (grade < -0.5%).
  - speed:     velocidad (km/h).
  - rear_teeth / rear_index: sobre puntos con datos de marcha Di2 (solo ciclismo).
  - power:     vatios, sobre puntos con potencia (en ciclismo hoy siempre
               n=0 -- ningun dispositivo del usuario la graba; en running SI
               hay datos reales, ver DISENO_ANALISIS_TRAMOS seccion 6).
"""

import gzip
import json
import math
import os
import time
from pathlib import Path

import settings_store

HERE = Path(__file__).resolve().parent
# BIKENALYSIS_DATA_DIR la fija app.py cuando corre empaquetado como .exe, para
# que los datos vivan junto al ejecutable y no en la carpeta temporal donde
# PyInstaller descomprime el codigo.
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"

# Bajo este numero de muestras en la ventana, una metrica concreta cae de
# vuelta al historico completo -- con menos que esto los percentiles salen
# demasiado ruidosos para calibrar nada (no es uno de los 3 settings
# expuestos en Ajustes, es un detalle de implementacion).
MIN_WINDOW_SAMPLES = 500


def percentile(sorted_vals, p):
    """Percentil p (0-100) por interpolacion lineal, sobre una lista YA ordenada."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return d0 + d1


PCTS = (5, 10, 25, 50, 75, 90, 95)


def describe(values):
    if not values:
        return {"n": 0}
    vals = sorted(values)
    out = {"n": len(vals), "min": vals[0], "max": vals[-1],
           "mean": sum(vals) / len(vals)}
    for p in PCTS:
        out[f"p{p}"] = percentile(vals, p)
    return out


class Metric:
    """Acumula cada valor en dos listas (ventana reciente + historico
    completo) segun se va recorriendo, y al pedir el resultado usa la
    ventana si tiene masa suficiente o cae de vuelta al historico si no."""

    def __init__(self):
        self.window = []
        self.all = []

    def add(self, value, in_window):
        self.all.append(value)
        if in_window:
            self.window.append(value)

    def result(self):
        used_window = len(self.window) >= MIN_WINDOW_SAMPLES
        out = describe(self.window if used_window else self.all)
        out["used_window"] = used_window
        return out


def _compute_baselines(records_by_ride, window_days, cutoff_ts, n_rides_total):
    hr = Metric()
    cadence = Metric()
    grade_up = Metric()
    grade_down = Metric()
    speed = Metric()
    rear_teeth = Metric()
    rear_index = Metric()
    power = Metric()

    # Contextuales "en subida": necesarios porque HR/cadencia/pinon en llano o
    # bajada no son comparables a lo que es normal DURANTE una subida. El corte
    # de "subida" se recalcula en dos pasadas (primero se necesita el propio
    # baseline de grade_up_pct para fijar el umbral de forma adaptativa).
    climbing_hr = Metric()
    climbing_cadence = Metric()
    climbing_rear_teeth = Metric()
    climbing_speed = Metric()
    climbing_power = Metric()

    # Contextuales "en bajada" (espejo de las de subida, ver DISENO_ANALISIS_TRAMOS
    # seccion 2 "Cobertura de bajadas"): piñon grande + cadencia muy alta en
    # descenso indica que no se puede bajar el ritmo de pedaleo con esa marcha.
    descending_cadence = Metric()
    descending_rear_teeth = Metric()
    descending_speed = Metric()

    n_rides_with_gear = 0
    n_rides_with_cadence = 0
    n_rides_in_window = 0

    for records in records_by_ride:
        ride_has_gear = any("rear_teeth" in r for r in records)
        ride_has_cad = any(r.get("cadence", 0) for r in records)
        if ride_has_gear:
            n_rides_with_gear += 1
        if ride_has_cad:
            n_rides_with_cadence += 1
        if records and records[0].get("ts", 0) >= cutoff_ts:
            n_rides_in_window += 1

        for r in records:
            in_window = r.get("ts", 0) >= cutoff_ts
            if r.get("heart_rate") is not None:
                hr.add(r["heart_rate"], in_window)
            if r.get("cadence"):
                cadence.add(r["cadence"], in_window)
            if r.get("speed") is not None:
                speed.add(r["speed"] * 3.6, in_window)
            g = r.get("grade")
            if g is not None:
                if g > 0.5:
                    grade_up.add(g, in_window)
                elif g < -0.5:
                    grade_down.add(g, in_window)
            if r.get("rear_teeth") is not None:
                rear_teeth.add(r["rear_teeth"], in_window)
            if r.get("rear_index") is not None:
                rear_index.add(r["rear_index"], in_window)
            if r.get("power") is not None:
                power.add(r["power"], in_window)

    grade_up_baseline = grade_up.result()
    grade_down_baseline = grade_down.result()
    # umbral adaptativo de "subida de verdad": la mediana de TODAS las
    # pendientes positivas del usuario (no un valor generico inventado).
    climbing_grade_threshold = grade_up_baseline.get("p50") or 3.0
    # mismo criterio en espejo para "bajada de verdad" (la mediana ya sale
    # negativa, p.ej. -4.2, asi que se usa tal cual como umbral <=).
    descending_grade_threshold = grade_down_baseline.get("p50") or -3.0

    for records in records_by_ride:
        for r in records:
            g = r.get("grade")
            if g is None:
                continue
            in_window = r.get("ts", 0) >= cutoff_ts
            if g >= climbing_grade_threshold:
                if r.get("heart_rate") is not None:
                    climbing_hr.add(r["heart_rate"], in_window)
                if r.get("cadence"):
                    climbing_cadence.add(r["cadence"], in_window)
                if r.get("speed") is not None:
                    climbing_speed.add(r["speed"] * 3.6, in_window)
                if r.get("rear_teeth") is not None:
                    climbing_rear_teeth.add(r["rear_teeth"], in_window)
                if r.get("power") is not None:
                    climbing_power.add(r["power"], in_window)
            elif g <= descending_grade_threshold:
                if r.get("cadence"):
                    descending_cadence.add(r["cadence"], in_window)
                if r.get("rear_teeth") is not None:
                    descending_rear_teeth.add(r["rear_teeth"], in_window)
                if r.get("speed") is not None:
                    descending_speed.add(r["speed"] * 3.6, in_window)

    return {
        "n_rides_total": n_rides_total,
        "n_rides_in_window": n_rides_in_window,
        "baseline_window_days": window_days,
        "n_rides_with_gear_data": n_rides_with_gear,
        "n_rides_with_cadence_data": n_rides_with_cadence,
        "climbing_grade_threshold_pct": climbing_grade_threshold,
        "descending_grade_threshold_pct": descending_grade_threshold,
        "hr": hr.result(),
        "cadence": cadence.result(),
        "grade_up_pct": grade_up_baseline,
        "grade_down_pct": grade_down_baseline,
        "speed_kmh": speed.result(),
        "rear_teeth": rear_teeth.result(),
        "rear_index": rear_index.result(),
        # potencia: en ciclismo de momento siempre n=0 (ningun dispositivo del
        # usuario la graba), pero calculado igual para dejar el formato listo
        # (ver DISENO_ANALISIS_TRAMOS seccion 2). En running si hay datos reales.
        "power": power.result(),
        "climbing_power": climbing_power.result(),
        # baselines condicionados a "mientras se sube" (grade >= climbing_grade_threshold_pct)
        "climbing_hr": climbing_hr.result(),
        "climbing_cadence": climbing_cadence.result(),
        "climbing_speed_kmh": climbing_speed.result(),
        "climbing_rear_teeth": climbing_rear_teeth.result(),
        # baselines condicionados a "mientras se baja" (grade <= descending_grade_threshold_pct)
        "descending_cadence": descending_cadence.result(),
        "descending_rear_teeth": descending_rear_teeth.result(),
        "descending_speed_kmh": descending_speed.result(),
    }


def main():
    window_days = settings_store.load_settings()["baseline_window_days"]
    cutoff_ts = time.time() - window_days * 86400

    # Solo ciclismo y running tienen clasificador de tramos (piñon/cadencia/
    # grado propios de cada uno -- ver DISENO_ANALISIS_TRAMOS seccion 6/9).
    # Desde que strava_sync.py descarga tambien otros deportes (caminar,
    # gimnasio...) para que aporten su pulso al TRIMP (ver _sport_from_type),
    # hace falta filtrar aqui explicitamente -- sin este filtro se les
    # construiria un arbol de baseline sin sentido (grado/piñon vacios) que
    # ademas haria que interesting_points.py SI intentara clasificarlos.
    SUPPORTED_SPORTS = ("cycling", "running")
    records_by_sport = {"cycling": [], "running": []}
    for path in sorted(RECORDS_DIR.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        sport = d.get("sport", "cycling")
        if sport not in SUPPORTED_SPORTS:
            continue
        records_by_sport.setdefault(sport, []).append(d["records"])

    baselines = {
        sport: _compute_baselines(records_by_ride, window_days, cutoff_ts, len(records_by_ride))
        for sport, records_by_ride in records_by_sport.items()
        if records_by_ride
    }

    out_path = DATA_DIR / "baselines.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baselines, f, indent=2, ensure_ascii=False)

    for sport, b in baselines.items():
        print(f"--- {sport} ({b['n_rides_total']} actividades) ---")
        print(f"  climbing_grade_threshold_pct: {b['climbing_grade_threshold_pct']:.2f}")
        print(f"  hr n={b['hr']['n']}  cadence n={b['cadence']['n']}  power n={b['power']['n']}")
    print()
    print(f"Guardado en {out_path}")


if __name__ == "__main__":
    main()
