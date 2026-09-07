#!/usr/bin/env python3
"""
Indice ligero de rutas: para cada actividad, guarda solo un puñado de numeros
(punto de inicio, punto final, bounding box, distancia total) sacados de
data/records/*.json.gz. Sirve para descartar rapido, sin abrir cada fichero de
puntos completo, que pares de actividades NO pueden compartir ruta/tramos
(p.ej. si empiezan a 20km de distancia entre si, no hace falta comparar punto
a punto).
"""

import gzip
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"


def build_route_index():
    index = {}
    for path in sorted(RECORDS_DIR.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        records = [r for r in d["records"] if r.get("lat") is not None and r.get("lon") is not None]
        if not records:
            continue
        lats = [r["lat"] for r in records]
        lons = [r["lon"] for r in records]
        dists = [r["distance"] for r in records if r.get("distance") is not None]
        index[d["activity_id"]] = {
            "start_lat": records[0]["lat"],
            "start_lon": records[0]["lon"],
            "end_lat": records[-1]["lat"],
            "end_lon": records[-1]["lon"],
            "min_lat": min(lats), "max_lat": max(lats),
            "min_lon": min(lons), "max_lon": max(lons),
            "distance_m": (max(dists) - min(dists)) if dists else None,
            "n_points": len(records),
        }
    out_path = DATA_DIR / "route_index.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    print(f"{len(index)} rutas indexadas en {out_path}")
    return index


if __name__ == "__main__":
    build_route_index()
