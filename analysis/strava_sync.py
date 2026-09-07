#!/usr/bin/env python3
"""
Sincronizador de actividades Strava -> registros de Bikenalysis (sin FIT).

Uso:
    python strava_sync.py auth      # autoriza la app una vez (abre el navegador)
    python strava_sync.py sync      # descarga las actividades nuevas

Solo usa libreria estandar de Python 3 (nada de pip install). Configura
STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET en el fichero .env de la raiz del
proyecto (copia .env.example y rellena con lo que ves en
strava.com/settings/api tras crear una app -- ver README).

Diferencia clave con hammerhead_sync.py: la API publica de Strava NO da
acceso al fichero FIT original (verificado en su documentacion, ver
DISENO_ANALISIS_TRAMOS seccion 7) -- solo a "streams" (series paralelas de
GPS/altitud/pulso/cadencia/potencia/pendiente, ya calculadas por Strava). Por
eso este modulo no descarga .fit ni pasa por fit_parser.py: convierte esas
streams directamente al mismo formato interno de puntos que usa el resto del
motor (mismo esquema que produce fit_parser: ts/lat/lon/altitude/heart_rate/
cadence/distance/speed/grade/power) y escribe el mismo
data/records/<id>.json.gz + entrada en data/summaries.json que build_dataset.py
genera para las actividades de Hammerhead -- ambas fuentes conviven en el
mismo dataset sin que baselines.py / interesting_points.py necesiten saber de
donde vino cada una.

Consecuencia real de esta limitacion de la API: las actividades de bici que
vengan de Strava NUNCA tendran datos de marcha Di2 (has_gear_data=False
siempre), aunque la salida real se hiciera con Ki2 conectado -- ese dato solo
existe en el FIT nativo del Karoo, que Strava no expone. Por eso Hammerhead
sigue siendo la fuente preferida para bici; Strava aporta sobre todo
actividades que no esten ya en Hammerhead (de otros dispositivos, o running).
"""

import argparse
import datetime
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import fit_parser

HERE = Path(__file__).resolve().parent
# BIKENALYSIS_ROOT la fija app.py cuando corre empaquetado como .exe, para que
# .env/tokens/state vivan junto al ejecutable y no en la carpeta temporal
# donde PyInstaller descomprime el codigo (mismo mecanismo que hammerhead_sync.py).
ROOT = Path(os.environ.get("BIKENALYSIS_ROOT", HERE.parent))
ENV_FILE = ROOT / ".env"
TOKENS_FILE = ROOT / "strava_tokens.json"
STATE_FILE = ROOT / "strava_state.json"

DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"

AUTH_BASE = "https://www.strava.com/oauth"
API_BASE = "https://www.strava.com/api/v3"

STREAM_KEYS = "time,latlng,distance,altitude,velocity_smooth,heartrate,cadence,watts,grade_smooth"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REDIRECT_URI"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET") if not env.get(k)]
    if missing:
        sys.exit(f"Falta configurar {', '.join(missing)} en {ENV_FILE}")
    env.setdefault("STRAVA_REDIRECT_URI", "http://localhost:3002")
    return env


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"Error HTTP {e.code} llamando a {url}:\n{body}")


def get_json(url, access_token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"Error HTTP {e.code} llamando a {url}:\n{body}")


def get_json_optional(url, access_token, tolerate_status=(404,)):
    """Como get_json, pero devuelve None (sin cortar la sincronizacion
    entera) si la respuesta es uno de `tolerate_status` -- pasa de verdad con
    streams: una actividad puede aparecer en el listado y aun asi no tener
    streams (p.ej. una entrada manual sin GPS/sensores, verificado con datos
    reales), lo cual no es un fallo real como un token invalido o el limite
    de peticiones agotado."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in tolerate_status:
            return None
        body = e.read().decode(errors="replace")
        sys.exit(f"Error HTTP {e.code} llamando a {url}:\n{body}")


# ---------------------------------------------------------------- auth ----

def cmd_auth(env):
    state = secrets.token_urlsafe(16)
    redirect_uri = env["STRAVA_REDIRECT_URI"]
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 3002

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silencio en consola

        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Estado invalido, vuelve a intentarlo.")
                return
            if "code" in qs:
                result["code"] = qs["code"][0]
                msg = "Autorizado. Ya puedes cerrar esta pestana y volver a la terminal."
            else:
                result["error"] = qs.get("error", ["desconocido"])[0]
                msg = f"Autorizacion denegada: {result['error']}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

    # activity:read_all para que incluya tambien actividades marcadas "Solo yo"
    # (no solo publicas/seguidores, que es lo unico que da activity:read).
    auth_url = (
        f"{AUTH_BASE}/authorize?response_type=code"
        f"&client_id={urllib.parse.quote(env['STRAVA_CLIENT_ID'])}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        "&scope=activity%3Aread_all"
        f"&state={state}"
        "&approval_prompt=auto"
    )

    print("Abriendo el navegador para autorizar la app en Strava...")
    print(auth_url)
    webbrowser.open(auth_url)

    httpd = http.server.HTTPServer(("localhost", port), Handler)
    print(f"Esperando la autorizacion en {redirect_uri} ...")
    while "code" not in result and "error" not in result:
        httpd.handle_request()

    if "error" in result:
        sys.exit(f"Autorizacion denegada por Strava: {result['error']}")

    tokens = post_form(
        f"{API_BASE}/oauth/token",
        {
            "client_id": env["STRAVA_CLIENT_ID"],
            "client_secret": env["STRAVA_CLIENT_SECRET"],
            "grant_type": "authorization_code",
            "code": result["code"],
        },
    )
    save_json(TOKENS_FILE, tokens)
    print(f"Autorizado correctamente. Tokens guardados en {TOKENS_FILE}")


def ensure_access_token(env):
    tokens = load_json(TOKENS_FILE, None)
    if tokens is None:
        sys.exit("No hay tokens guardados. Ejecuta primero: python strava_sync.py auth")

    if int(time.time()) < tokens.get("expires_at", 0) - 60:
        return tokens["access_token"]

    print("El token ha caducado, renovando con el refresh_token...")
    # Strava rota el refresh_token en cada renovacion -- hay que guardar
    # siempre el que devuelve la respuesta, el anterior deja de servir.
    new_tokens = post_form(
        f"{API_BASE}/oauth/token",
        {
            "client_id": env["STRAVA_CLIENT_ID"],
            "client_secret": env["STRAVA_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    save_json(TOKENS_FILE, new_tokens)
    return new_tokens["access_token"]


# --------------------------------------------------------- conversion ----

def _sport_from_type(activity_type):
    """El tipo de actividad de Strava ('Ride', 'Run', 'TrailRun',
    'VirtualRide'...) se reduce a los dos deportes que soporta hoy el motor
    (ver DISENO_ANALISIS_TRAMOS seccion 6/9 -- selector de deporte completo
    queda para mas adelante). Cualquier otro tipo (nadar, andar...) se
    descarta por ahora, no porque no importe sino porque el motor de tramos
    no tiene todavia baselines/clasificador para ellos."""
    t = (activity_type or "").lower()
    if "run" in t:
        return "running"
    if "ride" in t or "bik" in t or "cycl" in t:
        return "cycling"
    return None


# Strava distingue el TIPO concreto de actividad (sport_type, mas fino que
# el generico 'type') -- carretera, montaña, gravel... Se traduce a una
# etiqueta legible para que la lista de actividades diga de que iba cada
# una, no solo si llevaba marcha Di2 (ver DISENO_ANALISIS_TRAMOS, quejaba
# el usuario de que la columna "Marcha" no servia de nada al mezclar fuentes).
SPORT_TYPE_LABELS = {
    "Ride": "Ciclismo carretera",
    "GravelRide": "Ciclismo gravel",
    "MountainBikeRide": "Ciclismo montaña",
    "EMountainBikeRide": "Ciclismo montaña (eléctrica)",
    "EBikeRide": "Ciclismo carretera (eléctrica)",
    "VirtualRide": "Ciclismo virtual",
    "Velomobile": "Ciclismo (velomóvil)",
    "Handcycle": "Handbike",
    "Run": "Running",
    "TrailRun": "Running trail",
    "VirtualRun": "Running virtual",
}


def _sport_detail_from_type(activity_type, sport_type):
    return SPORT_TYPE_LABELS.get(sport_type) or SPORT_TYPE_LABELS.get(activity_type) or activity_type


def _parse_iso_to_epoch(s):
    dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def streams_to_records(streams, sport, start_epoch):
    """Convierte el StreamSet de Strava (dict tipo -> {'data': [...]}, con
    key_by_type=true) al mismo formato de punto que usa el resto del motor.
    Todas las streams solicitadas vienen alineadas por indice (misma
    longitud), garantizado por la propia API de Strava."""
    times = (streams.get("time") or {}).get("data") or []
    n = len(times)
    if n == 0:
        return []

    latlng = (streams.get("latlng") or {}).get("data")
    distance = (streams.get("distance") or {}).get("data")
    altitude = (streams.get("altitude") or {}).get("data")
    heartrate = (streams.get("heartrate") or {}).get("data")
    cadence = (streams.get("cadence") or {}).get("data")
    watts = (streams.get("watts") or {}).get("data")
    grade = (streams.get("grade_smooth") or {}).get("data")
    velocity = (streams.get("velocity_smooth") or {}).get("data")

    records = []
    for i in range(n):
        rec = {"ts": start_epoch + times[i]}
        if latlng and latlng[i]:
            rec["lat"], rec["lon"] = latlng[i][0], latlng[i][1]
        if altitude and altitude[i] is not None:
            rec["altitude"] = altitude[i]
        if heartrate and heartrate[i] is not None:
            rec["heart_rate"] = heartrate[i]
        if cadence and cadence[i] is not None:
            # Strava tambien da la cadencia de carrera "de una pierna" (igual
            # que los relojes Amazfit/Garmin, ver fit_parser.py) -- se dobla
            # para pasos/min reales, igual que se hace con el FIT del reloj.
            rec["cadence"] = cadence[i] * 2.0 if sport == "running" else cadence[i]
        if distance and distance[i] is not None:
            rec["distance"] = distance[i]
        if velocity and velocity[i] is not None:
            rec["speed"] = velocity[i]
        if watts and watts[i] is not None:
            rec["power"] = watts[i]
        if grade and grade[i] is not None:
            rec["grade"] = grade[i]
        records.append(rec)
    return records


def _write_activity(activity_id, sport, sport_detail, records):
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    gz_path = RECORDS_DIR / f"{activity_id}.json.gz"
    import gzip
    with gzip.open(gz_path, "wt", encoding="utf-8") as f:
        json.dump({
            "activity_id": activity_id,
            "sport": sport,
            "records": records,
            "gear_events": [],  # Strava nunca da cambios de marcha Di2 -- ver cabecera
        }, f, separators=(",", ":"))

    summ = fit_parser.summarize({
        "sport": sport,
        "records": records,
        "n_front_shifts": 0,
        "n_rear_shifts": 0,
        "has_gear_data": False,
        "has_cadence": any("cadence" in r for r in records),
    })
    summ["activity_id"] = activity_id
    summ["filename"] = None
    summ["source"] = "strava"
    summ["sport_detail"] = sport_detail
    return summ


def _merge_into_summaries(new_summaries):
    """Fusiona (por activity_id) en data/summaries.json sin tocar las
    entradas de otras fuentes (Hammerhead) -- build_dataset.py hace la misma
    fusion en sentido contrario al reparsear FIT, ver ahi el porque."""
    path = DATA_DIR / "summaries.json"
    existing = {"summaries": [], "errors": []}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    by_id = {s["activity_id"]: s for s in existing.get("summaries", []) if s.get("activity_id")}
    for s in new_summaries:
        by_id[s["activity_id"]] = s
    summaries = sorted(by_id.values(), key=lambda s: s.get("start_ts") or 0)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summaries": summaries, "errors": existing.get("errors", [])}, f,
                   indent=2, ensure_ascii=False)


# --------------------------------------------------------- duplicados ----

# Si el mismo dispositivo (p.ej. el Karoo) sube la salida tanto a Hammerhead
# como a Strava (auto-subida, algo habitual), la misma actividad real
# aparceria dos veces en el dataset con dos activity_id distintos -- eso
# ademas de duplicar en la lista, contaria dos veces cada punto en los
# baselines. Se descarta como duplicada cualquier actividad de Strava cuya
# hora de inicio caiga a menos de esto de otra ya conocida (de cualquier
# fuente), sea del sync de hoy o de antes.
DUPLICATE_START_TOLERANCE_S = 120


def _known_start_times():
    path = DATA_DIR / "summaries.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    return [s["start_ts"] for s in d.get("summaries", []) if s.get("start_ts") is not None]


def _is_duplicate_start(start_epoch, known_starts):
    return any(abs(ts - start_epoch) <= DUPLICATE_START_TOLERANCE_S for ts in known_starts)


# ---------------------------------------------------------------- sync ----

def cmd_sync(env, log=print):
    """Descarga las actividades nuevas de Strava (streams, no FIT -- ver
    cabecera del fichero) y las escribe directamente como records.json.gz +
    entradas en summaries.json, igual que build_dataset.py hace para
    Hammerhead. Devuelve un resumen dict con new_count/new_activities."""
    access_token = ensure_access_token(env)
    state = load_json(STATE_FILE, {"downloaded_ids": [], "last_sync_epoch": None})
    downloaded = set(state.get("downloaded_ids", []))
    known_starts = _known_start_times()
    skipped_duplicate = 0

    after = state.get("last_sync_epoch")
    page = 1
    new_count = 0
    skipped_sport = 0
    new_activities = []
    new_summaries = []
    newest_epoch = after

    while True:
        params = {"page": page, "per_page": 100}
        if after:
            params["after"] = after
        url = f"{API_BASE}/athlete/activities?{urllib.parse.urlencode(params)}"
        items = get_json(url, access_token)
        if not items:
            break

        for item in items:
            activity_id = f"strava_{item['id']}"
            start_epoch = _parse_iso_to_epoch(item["start_date"])
            if newest_epoch is None or start_epoch > newest_epoch:
                newest_epoch = start_epoch

            if activity_id in downloaded:
                continue

            sport = _sport_from_type(item.get("type"))
            if sport is None:
                skipped_sport += 1
                downloaded.add(activity_id)  # no reintentar cada vez, ya se ha visto
                continue
            sport_detail = _sport_detail_from_type(item.get("type"), item.get("sport_type"))

            if _is_duplicate_start(start_epoch, known_starts):
                log(f"  (\"{item.get('name', activity_id)}\" parece la misma salida que ya "
                    "tienes de otra fuente -- misma hora de inicio, se salta)")
                skipped_duplicate += 1
                downloaded.add(activity_id)
                continue

            log(f"Descargando {item.get('name', activity_id)} ({item['start_date']})...")
            streams_url = f"{API_BASE}/activities/{item['id']}/streams?keys={STREAM_KEYS}&key_by_type=true"
            raw_streams = get_json_optional(streams_url, access_token)
            if raw_streams is None:
                log("  (sin streams disponibles para esta actividad -- entrada manual sin "
                    "GPS/sensores probablemente, se salta)")
                downloaded.add(activity_id)
                continue
            streams = {s["type"]: s for s in raw_streams} if isinstance(raw_streams, list) else raw_streams
            records = streams_to_records(streams, sport, start_epoch)
            if not records:
                log(f"  (sin puntos GPS/tiempo utilizables, se salta)")
                downloaded.add(activity_id)
                continue

            summ = _write_activity(activity_id, sport, sport_detail, records)
            new_summaries.append(summ)
            downloaded.add(activity_id)
            known_starts.append(start_epoch)  # para detectar duplicados entre si dentro de este mismo sync
            new_count += 1
            new_activities.append({
                "activity_id": activity_id,
                "name": item.get("name", activity_id),
                "sport": sport,
                "created_at": item["start_date"],
            })

        if len(items) < 100:
            break
        page += 1

    if new_summaries:
        _merge_into_summaries(new_summaries)

    state["downloaded_ids"] = sorted(downloaded)
    if newest_epoch:
        state["last_sync_epoch"] = newest_epoch
    save_json(STATE_FILE, state)

    log(f"Listo. {new_count} actividad(es) nueva(s) de Strava "
        f"({skipped_sport} descartada(s) por deporte no soportado todavia, "
        f"{skipped_duplicate} descartada(s) por ser duplicado de otra fuente).")
    return {"new_count": new_count, "new_activities": new_activities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Autoriza la app una vez con tu cuenta de Strava")
    sub.add_parser("sync", help="Descarga las actividades nuevas")

    # La consola de Windows suele usar cp1252, que no puede representar
    # cualquier caracter Unicode -- los nombres de actividad de Strava son
    # texto libre (a veces con emoji u otros caracteres fuera de ese
    # repertorio) y sin esto print() los tira con UnicodeEncodeError a mitad
    # de la sincronizacion (visto con datos reales). No afecta al modo app
    # (usa log_lines.append, no print), solo a la ejecucion por linea de
    # comandos.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    args = parser.parse_args()
    env = load_env()

    if args.command == "auth":
        cmd_auth(env)
    elif args.command == "sync":
        cmd_sync(env)


if __name__ == "__main__":
    main()
