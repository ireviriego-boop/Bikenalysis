#!/usr/bin/env python3
"""
Bikenalysis -- servidor local (solo libreria estandar de Python + pywebview
para la ventana).

Una sola app para sincronizar actividades nuevas de Hammerhead (boton
'Sincronizar' de la pagina, endpoint POST /api/sync) y analizarlas (mapa,
puntos interesantes, comparacion de tramos) -- ya no hace falta lanzar
hammerhead_sync.py por separado para el uso normal.

Uso:
    python app.py            # arranca el servidor y abre una ventana propia
    python app.py --build    # (re)genera el dataset antes de arrancar
    python app.py --port 9000
    python app.py --browser  # modo antiguo: abre una pestana del navegador
    python app.py --no-browser  # no abre nada, solo sirve (para depurar la API)

Empaquetado como Bikenalysis.exe (ver build_exe.bat), no hace falta tener
Python instalado: doble clic y se abre la ventana. En ese caso los datos
(fit_files/, data/, tokens.json...) se guardan junto al .exe, no en la
carpeta temporal donde PyInstaller descomprime el codigo -- por eso las
rutas se fijan mas abajo, antes de importar el resto de modulos.
"""

import argparse
import gzip
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# Recursos empaquetados de solo lectura (static/): en un .exe onefile viven en
# la carpeta temporal donde PyInstaller se autodescomprime (sys._MEIPASS); en
# desarrollo, junto a este fichero.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent
HERE = Path(__file__).resolve().parent if not FROZEN else BUNDLE_DIR
STATIC_DIR = BUNDLE_DIR / "static"

if FROZEN:
    # Datos de usuario (fit_files/, data/, tokens.json...): deben vivir junto
    # al .exe real, no en la carpeta temporal de arriba, o se perderian al
    # cerrar la app. Se fija via variables de entorno que ya usan/aceptan
    # hammerhead_sync.py y el resto de modulos de analysis/.
    APP_ROOT = Path(sys.executable).resolve().parent
    os.environ.setdefault("BIKENALYSIS_ROOT", str(APP_ROOT))
    os.environ.setdefault("BIKENALYSIS_DATA_DIR", str(APP_ROOT / "data"))
    os.environ.setdefault("BIKENALYSIS_FIT_DIR", str(APP_ROOT / "fit_files"))
else:
    APP_ROOT = HERE.parent  # donde vive hammerhead_sync.py, fit_files/, .env...

DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"
INTERESTING_DIR = DATA_DIR / "interesting_points"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(APP_ROOT))
import segment_match  # noqa: E402
import build_dataset  # noqa: E402
import baselines  # noqa: E402
import interesting_points  # noqa: E402
import route_index  # noqa: E402
import hammerhead_sync  # noqa: E402
import strava_sync  # noqa: E402
import settings_store  # noqa: E402
import profile_store  # noqa: E402
import trimp  # noqa: E402
import weather  # noqa: E402

# Solo una sincronizacion/reconstruccion a la vez (por si se pulsa el boton
# varias veces seguidas, o llega mas de una peticion al mismo tiempo).
sync_lock = threading.Lock()


def run_sync_and_rebuild(providers=None):
    """Descarga actividades nuevas de Hammerhead y/o Strava y actualiza el
    dataset de analisis (de forma incremental: solo se re-parsean los .fit
    nuevos). Pensado para llamarse desde el boton 'Sincronizar' de la web --
    por eso convierte cualquier fallo (token caducado, sin internet, etc.) en
    un resultado con 'ok': False en vez de tirar el hilo de la peticion abajo.

    `providers`: None sincroniza todas las fuentes conectadas (de siempre);
    una lista (p.ej. ["strava"]) restringe el sync a esas -- pensado para
    cuando el mismo GPS auto-sube la salida a mas de un sitio (p.ej. un
    Karoo que sube a Hammerhead y este a su vez reenvia a Strava) y el
    usuario prefiere traerla de una sola fuente en vez de confiar en la
    deteccion de duplicados."""
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "Ya hay una sincronizacion en curso, espera a que termine."}
    try:
        log_lines = []
        new_count = 0
        new_activities = []
        do_hammerhead = providers is None or "hammerhead" in providers
        do_strava = providers is None or "strava" in providers

        if do_hammerhead:
            try:
                env = hammerhead_sync.load_env()
                hh_result = hammerhead_sync.cmd_sync(env, log=log_lines.append)
                new_count += hh_result["new_count"]
                new_activities += hh_result["new_activities"]
            except SystemExit as e:
                return {"ok": False, "error": str(e) or "Fallo autorizando/descargando de Hammerhead.", "log": log_lines}

            # hammerhead_sync solo descarga el .fit -- su hora de inicio no
            # entra en summaries.json hasta que build_dataset.main() lo
            # parsea (mas abajo). Si eso se dejara para el final, la
            # deteccion de duplicados de Strava (ver
            # strava_sync._is_duplicate_start, que compara contra
            # summaries.json) compararia contra una version desactualizada y
            # no veria la actividad de Hammerhead recien bajada en este mismo
            # sync -- exactamente el caso real que colaba duplicados: la
            # misma salida subida a Hammerhead y Strava a la vez,
            # sincronizada de un tiron, acababa dos veces (una por fuente).
            # Por eso se vuelca aqui antes de mirar Strava; build_dataset es
            # incremental, así que este parseo extra de los .fit nuevos de
            # Hammerhead no se repite en la llamada final de mas abajo.
            if hh_result["new_count"]:
                build_dataset.main(log=log_lines.append)

        # Strava es opcional: si no esta configurado en .env todavia o no se
        # ha autorizado (python strava_sync.py auth), se salta sin romper la
        # sincronizacion de Hammerhead -- load_env()/cmd_sync() hacen
        # sys.exit() en ese caso, igual que hammerhead_sync.py.
        if do_strava:
            try:
                strava_env = strava_sync.load_env()
                strava_result = strava_sync.cmd_sync(strava_env, log=log_lines.append)
                new_count += strava_result["new_count"]
                new_activities += strava_result["new_activities"]
            except SystemExit as e:
                log_lines.append(f"Strava no sincronizado ({e}) -- omitido.")

        build_result = build_dataset.main(log=log_lines.append)
        baselines.main()
        interesting_points.main()
        route_index.build_route_index()
        trimp.compute_fitness_series()

        return {
            "ok": True,
            "new_count": new_count,
            "new_activities": new_activities,
            "n_total": build_result["n_total"],
            "errors": build_result["errors"],
            "log": log_lines,
        }
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        sync_lock.release()


def save_settings_and_recalc(partial):
    """Guarda los settings de Ajustes y recalcula baselines + tramos
    interesantes con ellos (el segmentador y la ventana movil del baseline
    los leen en el momento de llamarse -- ver settings_store.py). No hace
    falta volver a parsear los .fit ni el indice de rutas, esos no dependen
    de estos 3 settings."""
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "Ya hay una sincronizacion/recalculo en curso, espera a que termine."}
    try:
        settings = settings_store.save_settings(partial)
        baselines.main()
        interesting_points.main()
        return {"ok": True, "settings": settings}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        sync_lock.release()


def _recalc_in_background(fn):
    """Lanza `fn` (trimp.compute_fitness_series / interesting_points.main)
    en un hilo aparte, esperando su turno del mismo candado que usa
    Sincronizar/Ajustes para no pisar ficheros a medio escribir con un
    recalculo simultaneo. No bloquea la respuesta HTTP -- guardar un dato de
    perfil/peso debe notarse instantaneo, el recalculo (10-15s sobre todo el
    historico) es mantenimiento de fondo, no algo que el usuario deba
    esperar mirando la pantalla."""
    def run():
        with sync_lock:
            try:
                fn()
            except Exception as e:
                print(f"Fallo en recalculo de fondo: {e!r}")
    threading.Thread(target=run, daemon=True).start()


def save_profile_and_recalc(partial):
    """Guarda el perfil fisico (Ajustes) al instante y recalcula Fitness/
    Fatiga/Forma en segundo plano (el pulso en reposo lo usa -- ver
    trimp.py; la altura no afecta a ningun calculo del servidor, solo
    normaliza la zancada en la propia pagina)."""
    profile = profile_store.save_profile(partial)
    _recalc_in_background(trimp.compute_fitness_series)
    return {"ok": True, "profile": profile}


def save_weight_and_recalc(iso_date, weight_kg):
    """Añade una pesada al historial al instante; el vatios/kg de los tramos
    de running con potencia (ver interesting_points.py) se recalcula en
    segundo plano."""
    profile = profile_store.add_weight_entry(iso_date, weight_kg)
    _recalc_in_background(interesting_points.main)
    return {"ok": True, "profile": profile}


def delete_weight_and_recalc(iso_date):
    profile = profile_store.delete_weight_entry(iso_date)
    _recalc_in_background(interesting_points.main)
    return {"ok": True, "profile": profile}


# --------------------------------------------------- conexiones (Ajustes) --
# Para que otra persona pueda usar su propia copia de la app con sus propios
# datos (Hammerhead/Strava son cuentas personales) sin tocar la consola --
# "Conectar" en Ajustes lanza el mismo flujo de autorizacion que antes solo
# se podia hacer con "python hammerhead_sync.py auth" / "strava_sync.py auth".

_CONNECTIONS = {"hammerhead": hammerhead_sync, "strava": strava_sync}
_auth_in_progress = {"hammerhead": False, "strava": False}


def _is_configured(mod):
    try:
        mod.load_env()
        return True
    except SystemExit:
        return False


def connections_status():
    return {
        name: {
            "configured": _is_configured(mod),
            "connected": mod.TOKENS_FILE.exists(),
        }
        for name, mod in _CONNECTIONS.items()
    }


def _run_auth_background(name, mod, env):
    try:
        mod.cmd_auth(env)
    except Exception as e:
        print(f"Fallo autorizando {name}: {e!r}")
    finally:
        _auth_in_progress[name] = False


def start_connect(name):
    mod = _CONNECTIONS.get(name)
    if mod is None:
        return {"ok": False, "error": "Servicio desconocido."}
    if _auth_in_progress.get(name):
        return {"ok": False, "error": "Ya hay una autorización en curso para este servicio."}
    try:
        env = mod.load_env()
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    _auth_in_progress[name] = True
    threading.Thread(target=_run_auth_background, args=(name, mod, env), daemon=True).start()
    return {"ok": True, "message": "Se ha abierto tu navegador -- completa el login y vuelve aquí."}


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def load_summaries():
    with open(DATA_DIR / "summaries.json", encoding="utf-8") as f:
        return json.load(f)


def load_records(activity_id):
    path = RECORDS_DIR / f"{activity_id}.json.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_interesting(activity_id):
    path = INTERESTING_DIR / f"{activity_id}.json"
    if not path.exists():
        return {"activity_id": activity_id, "episodes": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencio en consola, salvo errores explicitos

    def _send(self, status, body_bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, obj, status=200):
        self._send(status, _json_bytes(obj))

    def _send_static(self, rel_path):
        if rel_path in ("", "/"):
            rel_path = "index.html"
        rel_path = rel_path.lstrip("/")
        path = (STATIC_DIR / rel_path).resolve()
        if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
            self._send(403, b"forbidden", "text/plain")
            return
        if not path.exists() or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = "text/html"
        if path.suffix == ".js":
            ctype = "application/javascript"
        elif path.suffix == ".css":
            ctype = "text/css"
        self._send(200, path.read_bytes(), ctype)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/api/rides":
                self._send_json(load_summaries())
                return

            if parsed.path == "/api/settings":
                self._send_json(settings_store.load_settings())
                return

            if parsed.path == "/api/connections":
                self._send_json(connections_status())
                return

            if parsed.path == "/api/profile":
                self._send_json(profile_store.load_profile())
                return

            if parsed.path == "/api/fitness":
                self._send_json(trimp.load_fitness_summary())
                return

            if parsed.path == "/api/weather":
                # viento/temperatura informativos del inicio de la actividad
                # (ver weather.py) -- se piden bajo demanda, no en cada sync,
                # para no golpear el servicio externo por cientos de actividades
                # a la vez cuando la mayoria de veces nadie las va a abrir.
                activity_id = qs.get("activity_id", [None])[0]
                if not activity_id:
                    self._send_json({"error": "falta activity_id"}, 400)
                    return
                data = load_records(activity_id)
                if data is None:
                    self._send_json({"error": "actividad no encontrada"}, 404)
                    return
                records = data["records"]
                start = next((r for r in records if r.get("lat") is not None and r.get("lon") is not None), None)
                if not start or not records[0].get("ts"):
                    self._send_json({"error": "sin datos de posición para esta actividad"}, 400)
                    return
                result = weather.fetch_weather_for_activity(activity_id, start["lat"], start["lon"], records[0]["ts"])
                self._send_json(result or {"error": "sin datos meteorológicos disponibles"})
                return

            if parsed.path.startswith("/api/ride/"):
                # el segmento de la ruta viene tal cual en la peticion HTTP
                # (percent-encoded si lleva caracteres no-ASCII, p.ej. una "ñ"
                # en un id sacado de un nombre de fichero suelto) -- hay que
                # decodificarlo, a diferencia de qs (parse_qs ya lo hace solo).
                activity_id = urllib.parse.unquote(parsed.path[len("/api/ride/"):])
                data = load_records(activity_id)
                if data is None:
                    self._send_json({"error": "actividad no encontrada"}, 404)
                    return
                # el track completo (1 punto/seg) es mucho para mandar al
                # navegador en rutas largas -- se reduce para el mapa/graficas,
                # las estadisticas ya se calcularon con la resolucion completa.
                pts = data["records"]
                max_points = 3000
                stride = max(1, len(pts) // max_points)
                data["track"] = pts[::stride]
                del data["records"]
                interesting = load_interesting(activity_id)
                data["interesting_points"] = interesting["episodes"]
                if pts and pts[0].get("ts"):
                    ride_date = datetime.fromtimestamp(pts[0]["ts"]).date().isoformat()
                    data["fatiga_context"] = trimp.forma_context_for_date(ride_date)
                self._send_json(data)
                return

            if parsed.path == "/api/tramo-history":
                activity_id = qs.get("activity_id", [None])[0]
                start_d = qs.get("start_distance_m", [None])[0]
                end_d = qs.get("end_distance_m", [None])[0]
                if not activity_id or start_d is None or end_d is None:
                    self._send_json({"error": "faltan parametros activity_id/start_distance_m/end_distance_m"}, 400)
                    return
                result = segment_match.find_segment_history(activity_id, float(start_d), float(end_d))
                self._send_json(result)
                return

            if parsed.path == "/api/tramo-custom":
                # analisis de un tramo elegido a mano por el usuario
                # (arrastrando sobre la grafica), con el mismo motor que los
                # tramos automaticos -- ver interesting_points.analyze_custom_range.
                activity_id = qs.get("activity_id", [None])[0]
                start_d = qs.get("start_distance_m", [None])[0]
                end_d = qs.get("end_distance_m", [None])[0]
                if not activity_id or start_d is None or end_d is None:
                    self._send_json({"error": "faltan parametros activity_id/start_distance_m/end_distance_m"}, 400)
                    return
                data = load_records(activity_id)
                if data is None:
                    self._send_json({"error": "actividad no encontrada"}, 404)
                    return
                sport = data.get("sport", "cycling")
                baselines_by_sport = interesting_points.load_baselines()
                baselines = baselines_by_sport.get(sport)
                if not baselines:
                    self._send_json({"error": "todavía no hay baseline suficiente para este deporte"}, 400)
                    return
                records = data["records"]
                profile = profile_store.load_profile()
                weight_kg = None
                if records and records[0].get("ts"):
                    ride_date = datetime.fromtimestamp(records[0]["ts"]).date().isoformat()
                    weight_kg = profile_store.weight_at_date(ride_date, profile)
                episode = interesting_points.analyze_custom_range(
                    records, float(start_d), float(end_d), baselines, sport, weight_kg)
                episode["activity_id"] = activity_id
                episode["sport"] = sport
                self._send_json(episode)
                return

            if parsed.path == "/api/compare":
                a = qs.get("a", [None])[0]
                b = qs.get("b", [None])[0]
                if not a or not b:
                    self._send_json({"error": "faltan parametros a y b"}, 400)
                    return
                result = segment_match.compare_activities(a, b)
                self._send_json(result)
                return

            self._send_static(parsed.path)
        except Exception as e:
            self._send_json({"error": repr(e)}, 500)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/sync":
                body = self._read_json_body()
                providers = body.get("providers")  # None = todas las conectadas
                result = run_sync_and_rebuild(providers)
                self._send_json(result, 200 if result.get("ok") else 500)
                return
            if parsed.path == "/api/settings":
                body = self._read_json_body()
                result = save_settings_and_recalc(body)
                self._send_json(result, 200 if result.get("ok") else 500)
                return
            if parsed.path == "/api/profile":
                body = self._read_json_body()
                result = save_profile_and_recalc(body)
                self._send_json(result, 200 if result.get("ok") else 500)
                return
            if parsed.path == "/api/profile/weight":
                body = self._read_json_body()
                iso_date = body.get("date") or profile_store.today_iso()
                weight_kg = body.get("weight_kg")
                if not weight_kg:
                    self._send_json({"ok": False, "error": "Falta weight_kg."}, 400)
                    return
                result = save_weight_and_recalc(iso_date, weight_kg)
                self._send_json(result, 200 if result.get("ok") else 500)
                return
            if parsed.path == "/api/profile/weight/delete":
                body = self._read_json_body()
                iso_date = body.get("date")
                if not iso_date:
                    self._send_json({"ok": False, "error": "Falta date."}, 400)
                    return
                result = delete_weight_and_recalc(iso_date)
                self._send_json(result, 200 if result.get("ok") else 500)
                return
            if parsed.path.startswith("/api/connect/"):
                name = parsed.path[len("/api/connect/"):]
                result = start_connect(name)
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            self._send_json({"error": "not found"}, 404)
        except Exception as e:
            self._send_json({"error": repr(e)}, 500)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--build", action="store_true", help="Regenera el dataset antes de arrancar")
    ap.add_argument("--browser", action="store_true", help="Abre una pestana del navegador en vez de una ventana propia")
    ap.add_argument("--no-browser", action="store_true", help="No abre nada, solo sirve (para depurar la API)")
    args = ap.parse_args()

    if args.build or not (DATA_DIR / "summaries.json").exists():
        print("Generando dataset (fit_parser + baselines + interesting_points + route_index)...")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        build_dataset.main()
        baselines.main()
        interesting_points.main()
        route_index.build_route_index()
        trimp.compute_fitness_series()

    httpd = ThreadingHTTPServer(("localhost", args.port), Handler)
    url = f"http://localhost:{args.port}/"

    if args.no_browser:
        print(f"Bikenalysis corriendo en {url}  (Ctrl+C para parar)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nParando servidor...")
            httpd.shutdown()
        return

    if args.browser:
        print(f"Bikenalysis corriendo en {url}  (Ctrl+C para parar)")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nParando servidor...")
            httpd.shutdown()
        return

    # Modo por defecto: ventana propia (pywebview), sin pestana de navegador
    # ni consola visible. El servidor corre en un hilo aparte y se para solo
    # al cerrar la ventana.
    try:
        import webview
    except ImportError:
        print("pywebview no esta instalado -- abriendo en el navegador en su lugar.")
        print(f"Bikenalysis corriendo en {url}  (Ctrl+C para parar)")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
        return

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    try:
        window = webview.create_window("Bikenalysis", url, width=1280, height=860, min_size=(900, 600))
        # Arranca maximizada -- con una ventana fija de 1280x860 la app se ve
        # diminuta en un monitor grande. Maximizar se hace tras arrancar el
        # bucle nativo (webview.start(func)), no antes de crear la ventana,
        # o la mayoria de backends lo ignoran porque la ventana real aun no
        # existe. El CSS (clamp() sobre html) hace el resto: agranda textos y
        # paneles con el ancho de ventana real, sea portatil o monitor grande.
        webview.start(window.maximize)
    except Exception as e:
        # Si el motor de ventana nativa (WebView2 en Windows) no esta
        # disponible, mejor abrir el navegador que cerrarse en silencio.
        print(f"No se pudo abrir la ventana nativa ({e!r}), probando con el navegador...")
        webbrowser.open(url)
        try:
            while server_thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    if FROZEN:
        # Sin consola visible (--windowed), un fallo silencioso seria
        # imposible de diagnosticar -- se deja constancia en un fichero junto
        # al .exe.
        try:
            main()
        except Exception:
            import traceback
            (APP_ROOT / "bikenalysis_error.log").write_text(traceback.format_exc(), encoding="utf-8")
            raise
    else:
        main()
