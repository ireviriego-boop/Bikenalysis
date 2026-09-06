#!/usr/bin/env python3
"""
Sincronizador de actividades Hammerhead -> ficheros FIT locales.

Uso:
    python hammerhead_sync.py auth      # autoriza la app una vez (abre el navegador)
    python hammerhead_sync.py sync      # descarga las actividades nuevas como FIT

Solo usa librerias estandar de Python 3 (no hace falta instalar nada con pip).
Configura HH_CLIENT_ID / HH_CLIENT_SECRET en el fichero .env de esta misma carpeta
(copia .env.example y rellena con lo que ves en dashboard.hammerhead.io -> Ajustes -> Ajustes de la API).
"""

import argparse
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

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
TOKENS_FILE = HERE / "tokens.json"
STATE_FILE = HERE / "state.json"
FIT_DIR = HERE / "fit_files"

AUTH_BASE = "https://api.hammerhead.io/v1/auth"
API_BASE = "https://api.hammerhead.io/v1/api"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # las variables de entorno reales, si existen, tienen prioridad sobre el .env
    for k in ("HH_CLIENT_ID", "HH_CLIENT_SECRET", "HH_REDIRECT_URI", "HH_SCOPE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in ("HH_CLIENT_ID", "HH_CLIENT_SECRET") if not env.get(k)]
    if missing:
        sys.exit(f"Falta configurar {', '.join(missing)} en {ENV_FILE}")
    env.setdefault("HH_REDIRECT_URI", "http://localhost:3001")
    env.setdefault("HH_SCOPE", "activity:read")
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


def get_binary(url, access_token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"Error HTTP {e.code} llamando a {url}:\n{body}")


# ---------------------------------------------------------------- auth ----

def cmd_auth(env):
    state = secrets.token_urlsafe(16)
    redirect_uri = env["HH_REDIRECT_URI"]
    parsed = urllib.parse.urlparse(redirect_uri)
    port = parsed.port or 3001

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

    auth_url = (
        f"{AUTH_BASE}/oauth/authorize?response_type=code"
        f"&client_id={urllib.parse.quote(env['HH_CLIENT_ID'])}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={urllib.parse.quote(env['HH_SCOPE'])}"
        f"&state={state}"
    )

    print("Abriendo el navegador para autorizar la app en Hammerhead...")
    print(auth_url)
    webbrowser.open(auth_url)

    httpd = http.server.HTTPServer(("localhost", port), Handler)
    print(f"Esperando la autorizacion en {redirect_uri} ...")
    while "code" not in result and "error" not in result:
        httpd.handle_request()

    if "error" in result:
        sys.exit(f"Autorizacion denegada por Hammerhead: {result['error']}")

    tokens = post_form(
        f"{AUTH_BASE}/oauth/token",
        {
            "client_id": env["HH_CLIENT_ID"],
            "client_secret": env["HH_CLIENT_SECRET"],
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
        },
    )
    tokens["obtained_at"] = int(time.time())
    save_json(TOKENS_FILE, tokens)
    print(f"Autorizado correctamente. Tokens guardados en {TOKENS_FILE}")


def ensure_access_token(env):
    tokens = load_json(TOKENS_FILE, None)
    if tokens is None:
        sys.exit("No hay tokens guardados. Ejecuta primero: python hammerhead_sync.py auth")

    age = int(time.time()) - tokens.get("obtained_at", 0)
    if age < tokens.get("expires_in", 0) - 60:
        return tokens["access_token"]

    print("El token ha caducado, renovando con el refresh_token...")
    new_tokens = post_form(
        f"{AUTH_BASE}/oauth/token",
        {
            "client_id": env["HH_CLIENT_ID"],
            "client_secret": env["HH_CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    new_tokens["obtained_at"] = int(time.time())
    save_json(TOKENS_FILE, new_tokens)
    return new_tokens["access_token"]


# ---------------------------------------------------------------- sync ----

def cmd_sync(env, since=None):
    access_token = ensure_access_token(env)
    state = load_json(STATE_FILE, {"downloaded_ids": [], "last_sync": None})
    downloaded = set(state.get("downloaded_ids", []))

    start_date = since or state.get("last_sync")
    FIT_DIR.mkdir(exist_ok=True)

    page = 1
    new_count = 0
    newest_created_at = state.get("last_sync")

    while True:
        params = {"page": page, "perPage": 50}
        if start_date:
            params["startDate"] = start_date
        url = f"{API_BASE}/activities?{urllib.parse.urlencode(params)}"
        data = get_json(url, access_token)

        for item in data.get("data", []):
            activity_id = item["id"]
            created_at = item.get("createdAt", "")
            if newest_created_at is None or created_at > newest_created_at:
                newest_created_at = created_at

            if activity_id in downloaded:
                continue

            print(f"Descargando {item.get('name', activity_id)} ({created_at})...")
            fit_bytes = get_binary(f"{API_BASE}/activities/{activity_id}/file", access_token)

            safe_name = activity_id.replace("/", "_").replace(":", "_")
            fit_path = FIT_DIR / f"{safe_name}.fit"
            fit_path.write_bytes(fit_bytes)

            downloaded.add(activity_id)
            new_count += 1

        if page >= data.get("totalPages", 1):
            break
        page += 1

    state["downloaded_ids"] = sorted(downloaded)
    if newest_created_at:
        state["last_sync"] = newest_created_at
    save_json(STATE_FILE, state)

    print(f"Listo. {new_count} actividad(es) nueva(s) descargada(s) en {FIT_DIR}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="Autoriza la app una vez con tu cuenta de Hammerhead")
    sync_parser = sub.add_parser("sync", help="Descarga las actividades nuevas como FIT")
    sync_parser.add_argument("--since", help="Fecha desde la que sincronizar, formato YYYY-MM-DD")

    args = parser.parse_args()
    env = load_env()

    if args.command == "auth":
        cmd_auth(env)
    elif args.command == "sync":
        cmd_sync(env, since=getattr(args, "since", None))


if __name__ == "__main__":
    main()
