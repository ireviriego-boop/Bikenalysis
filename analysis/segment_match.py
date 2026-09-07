#!/usr/bin/env python3
"""
Comparacion de dos actividades: encuentra los tramos de carretera/camino que
tienen en comun (aunque el resto de la ruta sea distinto) y compara como se
rindio en cada tramo compartido -- velocidad, pulso, cadencia, marcha,
duracion.

Enfoque (sin librerias externas, solo stdlib):
  1. Se indexa la actividad "A" en una rejilla espacial (celdas de ~15m,
     aproximacion equirectangular local a la latitud media de la ruta).
  2. Cada punto de la actividad "B" se busca en esa rejilla (su celda y las 8
     vecinas) y se queda con el punto de A mas cercano si esta a <= radio_m.
  3. Los puntos de B que SI han encontrado pareja en A se agrupan en tramos
     continuos (tolerando huecos cortos de ruido de GPS), y se descartan los
     tramos mas cortos que min_segment_m.
  4. Para cada tramo se calculan estadisticas de A y de B por separado
     (distancia, desnivel, velocidad media, pulso medio, cadencia media,
     marcha media si hay) y se devuelven junto con las diferencias.

Esto cubre los dos casos que pidio el usuario:
  - misma ruta repetida  -> saldra (normalmente) un unico tramo compartido que
    cubre casi toda la actividad.
  - rutas distintas que comparten tramos por el camino -> saldran varios
    tramos compartidos mas cortos, con el resto de cada ruta sin pareja.
"""

import gzip
import json
import math
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"

CELL_SIZE_M = 15.0
MATCH_RADIUS_M = 12.0
MIN_SEGMENT_M = 300.0
MAX_GAP_POINTS = 5  # huecos cortos de gps/tunel que se tapan dentro de un tramo


def load_records(activity_id):
    path = RECORDS_DIR / f"{activity_id}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    return d["records"]


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _scales(records):
    lats = [r["lat"] for r in records if r.get("lat") is not None]
    ref_lat = sum(lats) / len(lats) if lats else 0.0
    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(ref_lat))
    return lat_scale, max(lon_scale, 1.0)


def build_grid(records, cell_size_m=CELL_SIZE_M):
    lat_scale, lon_scale = _scales(records)
    grid = {}
    for i, r in enumerate(records):
        if r.get("lat") is None or r.get("lon") is None:
            continue
        ix = int((r["lat"] * lat_scale) // cell_size_m)
        iy = int((r["lon"] * lon_scale) // cell_size_m)
        grid.setdefault((ix, iy), []).append(i)
    return grid, lat_scale, lon_scale


def match_points(records_b, records_a, grid_a, lat_scale, lon_scale,
                  cell_size_m=CELL_SIZE_M, radius_m=MATCH_RADIUS_M):
    """Para cada punto de B, el indice del punto de A mas cercano si esta a
    <= radius_m, si no None."""
    matches = [None] * len(records_b)
    for i, r in enumerate(records_b):
        if r.get("lat") is None or r.get("lon") is None:
            continue
        ix = int((r["lat"] * lat_scale) // cell_size_m)
        iy = int((r["lon"] * lon_scale) // cell_size_m)
        best_j, best_d = None, radius_m
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid_a.get((ix + dx, iy + dy), ()):
                    ra = records_a[j]
                    d = haversine_m(r["lat"], r["lon"], ra["lat"], ra["lon"])
                    if d <= best_d:
                        best_d = d
                        best_j = j
        matches[i] = best_j
    return matches


def _runs(matches, max_gap=MAX_GAP_POINTS):
    """Agrupa indices de B con match no-None en tramos continuos, tolerando
    huecos cortos."""
    runs = []
    n = len(matches)
    i = 0
    while i < n:
        if matches[i] is None:
            i += 1
            continue
        start = i
        j = i
        gap = 0
        while j + 1 < n:
            if matches[j + 1] is not None:
                j += 1
                gap = 0
                continue
            if gap < max_gap:
                gap += 1
                j += 1
                continue
            break
        # recorta el final del tramo si terminaba en huecos sin cerrar
        while j > start and matches[j] is None:
            j -= 1
        runs.append((start, j))
        i = j + 1 + gap if matches[j] is not None else j + 1
    return runs


def _segment_stats(records, i0, i1):
    pts = records[i0:i1 + 1]
    if not pts:
        return None
    ts0, ts1 = pts[0]["ts"], pts[-1]["ts"]
    dists = [p["distance"] for p in pts if p.get("distance") is not None]
    hrs = [p["heart_rate"] for p in pts if p.get("heart_rate") is not None]
    cads = [p["cadence"] for p in pts if p.get("cadence")]
    speeds = [p["speed"] for p in pts if p.get("speed") is not None]
    alts = [p["altitude"] for p in pts if p.get("altitude") is not None]
    rears = [p["rear_teeth"] for p in pts if p.get("rear_teeth") is not None]
    powers = [p["power"] for p in pts if p.get("power") is not None]
    gain = loss = 0.0
    for a, b in zip(alts, alts[1:]):
        d = b - a
        if d > 0:
            gain += d
        else:
            loss += -d
    return {
        "start_ts": ts0,
        "end_ts": ts1,
        "duration_s": ts1 - ts0,
        "distance_m": (max(dists) - min(dists)) if dists else None,
        "elevation_gain_m": gain,
        "elevation_loss_m": loss,
        "avg_speed_kmh": (sum(speeds) / len(speeds)) * 3.6 if speeds else None,
        "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "avg_cadence": sum(cads) / len(cads) if cads else None,
        "avg_rear_teeth": sum(rears) / len(rears) if rears else None,
        "avg_power": sum(powers) / len(powers) if powers else None,
        "n_points": len(pts),
        "start_lat": pts[0].get("lat"), "start_lon": pts[0].get("lon"),
        "end_lat": pts[-1].get("lat"), "end_lon": pts[-1].get("lon"),
        "path": _downsampled_path(pts),
    }


def _downsampled_path(pts, max_points=200):
    """Lista [[lat,lon], ...] reducida, para poder dibujar el tramo compartido
    en el mapa sin mandar cada punto de 1 en 1 segundo."""
    stride = max(1, len(pts) // max_points)
    return [[p["lat"], p["lon"]] for p in pts[::stride] if p.get("lat") is not None]


def _clean_trend(a_idxs, jitter_tol=50):
    """Descarta saltos de indice de A que no encajan con la tendencia dominante
    del tramo. Sin esto, un cruce concurrido cerca de casa que A pasa por otro
    sitio muy lejano de la ruta (p.ej. km 2 y km 50) puede colarse como un
    'match' puntual y disparar el rango [a0, a1] a casi toda la ruta A aunque
    solo sea ruido de un unico punto -- ver el caso real detectado al probar
    con dos actividades reales (tramo que salia con dist_a=55km, dist_b=7.8km).
    """
    if len(a_idxs) < 3:
        return a_idxs
    diffs = [b - a for a, b in zip(a_idxs, a_idxs[1:])]
    n_pos = sum(1 for d in diffs if d > 0)
    n_neg = sum(1 for d in diffs if d < 0)
    dominant = 1 if n_pos >= n_neg else -1

    cleaned = [a_idxs[0]]
    for a in a_idxs[1:]:
        prev = cleaned[-1]
        delta = a - prev
        if abs(delta) <= jitter_tol or (delta * dominant) > 0:
            cleaned.append(a)
        # si no, se descarta como salto espurio (no se actualiza 'prev')
    return cleaned


def compare_activities(activity_id_a, activity_id_b,
                        min_segment_m=MIN_SEGMENT_M):
    records_a = load_records(activity_id_a)
    records_b = load_records(activity_id_b)

    grid_a, lat_scale, lon_scale = build_grid(records_a)
    matches = match_points(records_b, records_a, grid_a, lat_scale, lon_scale)

    segments = []
    for (b0, b1) in _runs(matches):
        raw_a_idxs = [matches[k] for k in range(b0, b1 + 1) if matches[k] is not None]
        if not raw_a_idxs:
            continue
        a_idxs = _clean_trend(raw_a_idxs)
        a0, a1 = min(a_idxs), max(a_idxs)
        stat_b = _segment_stats(records_b, b0, b1)
        stat_a = _segment_stats(records_a, a0, a1)
        if stat_a is None or stat_b is None:
            continue
        seg_len = stat_b["distance_m"] or 0
        if seg_len < min_segment_m:
            continue
        # sanity check: si el tramo de A que "coincide" mide muchisimo mas que
        # el de B, no es un tramo compartido real sino puntos dispersos que
        # han enganchado con partes muy distintas de la ruta A (p.ej. una
        # zona con calles muy juntas cerca de casa) -- se descarta.
        a_len = stat_a["distance_m"] or 0
        if a_len > 2.5 * seg_len + 500:
            continue
        # sentido de recorrido: ¿los indices de A crecen o decrecen segun avanza B?
        first_third = a_idxs[: max(1, len(a_idxs) // 3)]
        last_third = a_idxs[-max(1, len(a_idxs) // 3):]
        direction = "same" if (sum(last_third) / len(last_third)) >= (sum(first_third) / len(first_third)) else "reverse"

        delta = None
        if stat_a["avg_speed_kmh"] is not None and stat_b["avg_speed_kmh"] is not None:
            delta = {
                "speed_kmh_b_minus_a": stat_b["avg_speed_kmh"] - stat_a["avg_speed_kmh"],
                "hr_b_minus_a": (stat_b["avg_hr"] - stat_a["avg_hr"])
                                if stat_a.get("avg_hr") is not None and stat_b.get("avg_hr") is not None else None,
                "duration_s_b_minus_a": stat_b["duration_s"] - stat_a["duration_s"],
            }

        segments.append({
            "direction_b_vs_a": direction,
            "a_range": [a0, a1],
            "b_range": [b0, b1],
            "a": stat_a,
            "b": stat_b,
            "delta": delta,
        })

    segments.sort(key=lambda s: s["b"]["distance_m"] or 0, reverse=True)

    return {
        "activity_a": activity_id_a,
        "activity_b": activity_id_b,
        "n_points_a": len(records_a),
        "n_points_b": len(records_b),
        "n_shared_segments": len(segments),
        "total_shared_distance_m": sum(s["b"]["distance_m"] or 0 for s in segments),
        "segments": segments,
    }


# ------------------------------------------------ historial de un tramo --

MIN_TRAMO_MATCH_COVERAGE = 0.6  # % de puntos del tramo que hace falta cubrir
TRAMO_MATCH_RADIUS_M = 15.0


def _load_route_index():
    path = DATA_DIR / "route_index.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _bbox_overlap(tramo_bbox, other_bbox, margin_m=80.0):
    """Solapan (con margen sobre el bbox, pequeño, del tramo) dos rectangulos
    lat/lon -- descarte rapido antes de abrir records completos de otra
    actividad."""
    mean_lat = (tramo_bbox["min_lat"] + tramo_bbox["max_lat"]) / 2
    lat_m = margin_m / 111320.0
    lon_m = margin_m / (111320.0 * max(0.2, math.cos(math.radians(mean_lat))))
    return not (
        tramo_bbox["max_lat"] + lat_m < other_bbox["min_lat"] or
        other_bbox["max_lat"] < tramo_bbox["min_lat"] - lat_m or
        tramo_bbox["max_lon"] + lon_m < other_bbox["min_lon"] or
        other_bbox["max_lon"] < tramo_bbox["min_lon"] - lon_m
    )


def find_segment_history(activity_id, start_distance_m, end_distance_m, max_results=8):
    """Busca, entre el resto de actividades ya descargadas, otras veces que
    se haya pasado por este mismo tramo concreto de carretera (no la ruta
    entera) -- para poder comparar como fue cada vez (velocidad, pulso,
    cadencia...). Usa route_index.json para descartar de un vistazo las
    actividades cuya bounding box ni se acerca al tramo, y solo abre los
    records completos de las que de verdad podrian solapar."""
    records = load_records(activity_id)
    tramo_pts = [r for r in records
                 if r.get("distance") is not None and r.get("lat") is not None
                 and start_distance_m <= r["distance"] <= end_distance_m]
    if len(tramo_pts) < 2:
        return {"activity_id": activity_id, "matches": []}

    lats = [p["lat"] for p in tramo_pts]
    lons = [p["lon"] for p in tramo_pts]
    tramo_bbox = {"min_lat": min(lats), "max_lat": max(lats),
                  "min_lon": min(lons), "max_lon": max(lons)}

    candidates = [
        other_id for other_id, bbox in _load_route_index().items()
        if other_id != activity_id and _bbox_overlap(tramo_bbox, bbox)
    ]

    results = []
    for other_id in candidates:
        try:
            other_records = load_records(other_id)
        except FileNotFoundError:
            continue
        grid, lat_scale, lon_scale = build_grid(other_records)
        matches = match_points(tramo_pts, other_records, grid, lat_scale, lon_scale,
                                radius_m=TRAMO_MATCH_RADIUS_M)
        runs = _runs(matches, max_gap=3)
        if not runs:
            continue
        b0, b1 = max(runs, key=lambda r: r[1] - r[0])
        coverage = (b1 - b0 + 1) / len(tramo_pts)
        if coverage < MIN_TRAMO_MATCH_COVERAGE:
            continue
        a_idxs = [matches[k] for k in range(b0, b1 + 1) if matches[k] is not None]
        if not a_idxs:
            continue
        # descarta pasadas en sentido contrario (p.ej. una bajada emparejando
        # con este mismo tramo recorrido de subida) -- comparar velocidad/
        # pulso de una contra la otra no tendria sentido.
        first_third = a_idxs[: max(1, len(a_idxs) // 3)]
        last_third = a_idxs[-max(1, len(a_idxs) // 3):]
        same_direction = (sum(last_third) / len(last_third)) >= (sum(first_third) / len(first_third))
        if not same_direction:
            continue
        a_idxs = _clean_trend(a_idxs)
        a0, a1 = min(a_idxs), max(a_idxs)
        stat = _segment_stats(other_records, a0, a1)
        if stat is None:
            continue
        results.append({
            "activity_id": other_id,
            "start_ts": stat["start_ts"],
            "coverage": coverage,
            "duration_s": stat["duration_s"],
            "distance_m": stat["distance_m"],
            "avg_speed_kmh": stat["avg_speed_kmh"],
            "avg_hr": stat["avg_hr"],
            "avg_cadence": stat["avg_cadence"],
            "avg_rear_teeth": stat["avg_rear_teeth"],
            "avg_power": stat["avg_power"],
        })

    results.sort(key=lambda r: r["start_ts"] or 0, reverse=True)
    return {
        "activity_id": activity_id,
        "start_distance_m": start_distance_m,
        "end_distance_m": end_distance_m,
        "matches": results[:max_results],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("uso: python segment_match.py <activity_id_a> <activity_id_b>")
        sys.exit(1)
    result = compare_activities(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
