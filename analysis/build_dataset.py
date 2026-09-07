#!/usr/bin/env python3
"""
Recorre todos los ficheros .fit de fit_files/, los parsea con fit_parser,
y genera:

  data/summaries.json          -- resumen ligero de cada actividad (para
                                   listar rutas, comparar, calcular baselines)
  data/records/<activity>.json.gz  -- puntos completos de cada actividad,
                                   comprimidos (para el mapa / comparacion de
                                   segmentos), cargados solo bajo demanda.

Solo libreria estandar (json, gzip, pathlib) -- nada de pip install.
"""

import gzip
import json
import os
import sys
import traceback
from pathlib import Path

import fit_parser

HERE = Path(__file__).resolve().parent
# Por defecto fit_files/ vive junto a hammerhead_sync.py, un nivel por encima
# de esta carpeta analysis/. Se puede forzar otra ruta con la variable de
# entorno BIKENALYSIS_FIT_DIR (usado en el sandbox de desarrollo).
FIT_DIR = Path(os.environ.get("BIKENALYSIS_FIT_DIR", HERE.parent / "fit_files"))
# BIKENALYSIS_DATA_DIR la fija app.py cuando corre empaquetado como .exe, para
# que los datos vivan junto al ejecutable y no en la carpeta temporal donde
# PyInstaller descomprime el codigo.
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"


def activity_id_from_filename(path):
    # 108845.activity.<uuid>.fit -> <uuid> (basta como id corto y unico)
    stem = path.stem  # quita .fit
    parts = stem.split(".")
    if len(parts) >= 3 and parts[1] == "activity":
        return parts[2]
    # ficheros sueltos (p.ej. exportados a mano de Strava/Zepp, no vienen del
    # sync de Hammerhead): el nombre de fichero tal cual puede llevar espacios
    # o parentesis (p.ej. "Carrera (1).fit"), que rompen la URL /api/ride/<id>
    # -- se sustituyen por guiones bajos para tener un id seguro.
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)


def _load_previous_summaries():
    path = DATA_DIR / "summaries.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {s["activity_id"]: s for s in d.get("summaries", []) if s.get("activity_id")}
    except Exception:
        return {}


def main(force=False, log=print):
    """Parsea los .fit y regenera data/summaries.json + data/records/*.json.gz.

    Por defecto (force=False) es INCREMENTAL: si una actividad ya tiene su
    records/<id>.json.gz generado de una vez anterior, se reutiliza su resumen
    en vez de volver a parsear el .fit -- para que sincronizar unas pocas
    actividades nuevas no implique re-parsear las ~100+ que ya llevas
    analizadas. Con force=True se re-parsea todo (util si se corrige el
    parser).

    Devuelve un resumen dict; `log` recibe las lineas de progreso (por
    defecto print(), la app web pasa otra cosa para no mezclarlo con su
    propia consola).
    """
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    fit_files = sorted(FIT_DIR.glob("*.fit"))
    log(f"Encontrados {len(fit_files)} ficheros .fit")

    # Se carga siempre (aunque force=True) porque tambien sirve para
    # preservar actividades de otras fuentes sin .fit (ver mas abajo) --
    # force solo controla si se reutiliza el resumen cacheado de un .fit ya
    # parseado antes, no si se recuerdan esas otras entradas.
    previous = _load_previous_summaries()

    summaries = []
    errors = []
    n_parsed = 0
    n_reused = 0

    for i, path in enumerate(fit_files, 1):
        aid = activity_id_from_filename(path)
        gz_path = RECORDS_DIR / f"{aid}.json.gz"

        if not force and aid in previous and gz_path.exists():
            summaries.append(previous[aid])
            n_reused += 1
            continue

        try:
            act = fit_parser.parse_activity(path)
            summ = fit_parser.summarize(act)
            summ["activity_id"] = aid
            summ["filename"] = path.name
            summaries.append(summ)

            # puntos completos, comprimidos, para uso posterior (mapa, comparacion)
            with gzip.open(gz_path, "wt", encoding="utf-8") as f:
                json.dump({
                    "activity_id": aid,
                    "sport": act["sport"],
                    "records": act["records"],
                    "gear_events": act["gear_events"],
                }, f, separators=(",", ":"))

            n_parsed += 1
            log(f"[{i}/{len(fit_files)}] OK  {path.name}  "
                f"{summ.get('distance_km', 0):.1f} km, "
                f"{summ.get('n_points', 0)} pts")
        except Exception as e:
            errors.append({"filename": path.name, "error": repr(e)})
            log(f"[{i}/{len(fit_files)}] ERROR {path.name}: {e}")
            traceback.print_exc()

    # Actividades de otras fuentes sin fichero .fit (p.ej. Strava, ver
    # strava_sync.py -- descarga "streams" y escribe records.json.gz +
    # summaries.json directamente, sin pasar por aqui). Como este bucle solo
    # recorre fit_files/, si no se preservan explicitamente esas entradas
    # "huerfanas" (que SI estaban en el summaries.json anterior pero no
    # corresponden a ningun .fit actual) se perderian cada vez que se
    # sincroniza o recalcula algo de Hammerhead.
    fit_ids = {activity_id_from_filename(p) for p in fit_files}
    already_in_summaries = {s["activity_id"] for s in summaries}
    n_other_sources = 0
    for aid, s in previous.items():
        if aid not in fit_ids and aid not in already_in_summaries:
            summaries.append(s)
            n_other_sources += 1

    summaries.sort(key=lambda s: s.get("start_ts") or 0)

    with open(DATA_DIR / "summaries.json", "w", encoding="utf-8") as f:
        json.dump({"summaries": summaries, "errors": errors}, f, indent=2, ensure_ascii=False)

    log("")
    log(f"OK: {len(summaries)} actividades en total "
        f"({n_parsed} parseadas ahora, {n_reused} reutilizadas de antes).")
    log(f"Errores: {len(errors)}")
    for e in errors:
        log(f" - {e['filename']} {e['error']}")

    return {
        "n_total": len(summaries),
        "n_parsed": n_parsed,
        "n_reused": n_reused,
        "errors": errors,
    }


if __name__ == "__main__":
    force = "--force" in sys.argv
    main(force=force)
