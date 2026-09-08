#!/usr/bin/env python3
"""
Motor de "tramos interesantes" para Bikenalysis -- v2 (ver DISENO_ANALISIS_TRAMOS
en la raiz del proyecto, notas de diseno del 7 de septiembre de 2026).

Sustituye al motor anterior (punto a punto): antes se clasificaba cada
instante por separado y se agrupaban los puntos consecutivos con veredicto en
"episodios". El problema: un cambio de marcha anticipado generaba unos pocos
metros de cadencia muy alta o muy baja que no reflejaban un problema real, y el
analisis punto a punto los podia marcar como "atrancado" por error.

Ahora se hace al reves: primero se trocea la ruta en TRAMOS (seccion 1 del
diseno) segun rachas de cambios de piñon en la misma direccion y/o fases de
pendiente sostenida, y LUEGO se clasifica cada tramo entero por su media/
agregado -- un cambio de marcha aislado ya no puede generar un falso aviso de
un instante suelto, porque no se evalua instante a instante.

Cada tramo en subida o bajada de verdad (los llanos no tienen veredicto) se
clasifica con la mejor combinacion de señales disponible (jerarquia de tiers
de la seccion 2 del diseno, de mas a menos fiable):
  1. potencia (+ el resto si lo hay) -- sin datos reales todavia, ver mas abajo.
  2. piñon + cadencia + pulso + pendiente.
  3. piñon O cadencia (uno de los dos) + pulso + pendiente.
  4. solo pulso + pendiente.
  5. solo GPS + velocidad + pendiente (sin pulso).

El resultado no es solo avisos: un tramo puede salir "atrancado" (marcha
inadecuada) o "bien_ejecutado" (marcha/esfuerzo apropiados), y en subida con
datos de potencia se abre ademas la categoria "fatiga" (misma potencia de
siempre pero el pulso disparado). Todos los umbrales salen de
data/baselines.json (percentiles personales), nunca de valores inventados.
"""

import gzip
import json
import os
from datetime import datetime
from pathlib import Path

import profile_store
import settings_store

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"
OUT_DIR = DATA_DIR / "interesting_points"

# La divergencia maxima de pendiente y la distancia maxima de tramo se leen de
# data/settings.json (pantalla de Ajustes) en vez de ser constantes fijas --
# ver settings_store.py. MIN_DURATION_S y MIN_BASELINE_N no forman parte de
# los 3 settings expuestos (seccion 4 del diseno), se quedan fijos.

# Duracion minima de un tramo para reportarlo como episodio (evita ruido de
# tramos de un par de segundos en los bordes de una racha).
MIN_DURATION_S = 8

# Tamaño minimo de muestra de un baseline para confiar en sus percentiles.
MIN_BASELINE_N = 10

# Margen minimo exigido al cruzar un percentil, para dos motivos a la vez:
# 1) que el veredicto no dependa de un valor que practicamente EMPATA con el
#    umbral (ruido/redondeo, no una señal real).
# 2) que el texto no muestre "137 bpm, tu habitual hasta 137" -- el mismo
#    numero redondeado a los dos lados, que parece contradictorio aunque la
#    comparacion en si sea correcta (visto con datos reales).
HR_MARGIN_BPM = 1.5
CADENCE_MARGIN = 2.0
GEAR_MARGIN_TEETH = 1.0
SPEED_MARGIN_KMH = 0.3
POWER_MARGIN_W = 5.0


def load_baselines():
    with open(DATA_DIR / "baselines.json", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------- 1. segmentacion --

def segment_tramos(records, max_grade_divergence_pct=None, max_tramo_distance_m=None):
    """Trocea los puntos de una actividad en tramos (lista de (idx_inicio,
    idx_fin) inclusive), segun las reglas de la seccion 1 del diseno:

    - Si hay datos de piñon: un tramo se extiende mientras los cambios de
      piñon sucesivos vayan en la misma direccion; en cuanto uno invierte la
      racha, se corta ahi. Sin datos de piñon, esta condicion simplemente
      nunca se dispara y el corte queda solo en manos de la pendiente.
    - Guarda de pendiente (aplica siempre, haya o no piñon): la pendiente de
      cada punto nuevo no puede alejarse mas de `max_grade_divergence_pct` de
      la media del tramo acumulada hasta ese punto.
    - Tope de distancia `max_tramo_distance_m` por si no salta ningun corte.

    Si no se pasan explicitos, se leen de data/settings.json (pantalla de
    Ajustes) en el momento de la llamada -- asi un cambio de settings se nota
    sin reiniciar nada, solo hace falta volver a lanzar el analisis.
    """
    if max_grade_divergence_pct is None or max_tramo_distance_m is None:
        s = settings_store.load_settings()
        if max_grade_divergence_pct is None:
            max_grade_divergence_pct = s["max_grade_divergence_pct"]
        if max_tramo_distance_m is None:
            max_tramo_distance_m = s["max_tramo_distance_m"]

    n = len(records)
    if n == 0:
        return []

    def seed(i):
        grade0 = records[i].get("grade")
        return {
            "start_distance": records[i].get("distance"),
            "grade_sum": grade0 if grade0 is not None else 0.0,
            "grade_n": 1 if grade0 is not None else 0,
            "gear_dir": None,
            "last_rear_teeth": records[i].get("rear_teeth"),
        }

    tramos = []
    idx0 = 0
    cur = seed(0)

    for i in range(1, n):
        r = records[i]
        grade = r.get("grade")
        rear_teeth = r.get("rear_teeth")
        dist = r.get("distance")

        cut = False

        avg_grade_so_far = (cur["grade_sum"] / cur["grade_n"]) if cur["grade_n"] else None
        if grade is not None and avg_grade_so_far is not None:
            if abs(grade - avg_grade_so_far) > max_grade_divergence_pct:
                cut = True

        if not cut and rear_teeth is not None and cur["last_rear_teeth"] is not None \
                and rear_teeth != cur["last_rear_teeth"]:
            direction = "up" if rear_teeth > cur["last_rear_teeth"] else "down"
            if cur["gear_dir"] is None:
                cur["gear_dir"] = direction
            elif direction != cur["gear_dir"]:
                cut = True

        if not cut and dist is not None and cur["start_distance"] is not None:
            if dist - cur["start_distance"] >= max_tramo_distance_m:
                cut = True

        if cut:
            tramos.append((idx0, i - 1))
            idx0 = i
            cur = seed(i)
            continue

        if grade is not None:
            cur["grade_sum"] += grade
            cur["grade_n"] += 1
        if rear_teeth is not None:
            cur["last_rear_teeth"] = rear_teeth

    tramos.append((idx0, n - 1))
    return tramos


def _mean(vals):
    return sum(vals) / len(vals) if vals else None


def summarize_tramo(records, idx0, idx1):
    """Agregados de un tramo (idx0..idx1 inclusive) -- media/duracion/etc.,
    sin clasificar todavia. Sirve tanto para clasificar como para pintarlo."""
    pts = records[idx0:idx1 + 1]
    ts0, ts1 = pts[0]["ts"], pts[-1]["ts"]
    mid = pts[len(pts) // 2]

    grades = [p["grade"] for p in pts if p.get("grade") is not None]
    hrs = [p["heart_rate"] for p in pts if p.get("heart_rate") is not None]
    cads = [p["cadence"] for p in pts if p.get("cadence")]
    rears = [p["rear_teeth"] for p in pts if p.get("rear_teeth") is not None]
    powers = [p["power"] for p in pts if p.get("power") is not None]
    speeds = [p["speed"] for p in pts if p.get("speed") is not None]
    dists = [p["distance"] for p in pts if p.get("distance") is not None]

    return {
        "start_ts": ts0,
        "end_ts": ts1,
        "duration_s": ts1 - ts0,
        "lat": mid.get("lat"),
        "lon": mid.get("lon"),
        "start_distance_m": dists[0] if dists else None,
        "end_distance_m": dists[-1] if dists else None,
        "distance_m": (dists[0] + dists[-1]) / 2 if dists else None,
        "avg_grade_pct": _mean(grades),
        "avg_hr": _mean(hrs),
        "avg_cadence": _mean(cads),
        "avg_rear_teeth": _mean(rears),
        "avg_power": _mean(powers),
        "avg_speed_kmh": _mean(speeds) * 3.6 if speeds else None,
        "n_points": len(pts),
    }


# ------------------------------------------------------- 2. clasificacion --

def _band(bl, key):
    """Percentiles p25/p75/p90 de un baseline, o None si no hay masa
    suficiente para confiar en ellos."""
    b = bl.get(key) or {}
    if b.get("n", 0) < MIN_BASELINE_N:
        return None
    return b


def _classify_climb(agg, bl, sport, weight_kg=None):
    hr, cadence, rear, power = agg["avg_hr"], agg["avg_cadence"], agg["avg_rear_teeth"], agg["avg_power"]
    have_hr = hr is not None
    have_cadence = cadence is not None and cadence > 0
    have_gear = rear is not None
    have_power = power is not None

    # Tier 1 -- potencia: hoy nunca hay datos reales en ciclismo (ver cabecera
    # del fichero), asi que este bloque solo se activa con running, pero la
    # logica esta lista para cuando exista un medidor de bici tambien. Abre la
    # categoria nueva de "fatiga" (MISMA potencia de siempre, pulso disparado
    # -- no mas potencia, eso seria solo "esfuerzo mas duro" y explicaria el
    # pulso alto por si solo, no fatiga); el resto del veredicto
    # (atrancado/bien) lo deciden igualmente piñon/cadencia/pulso mas abajo.
    cad_unit = "zpm" if sport == "running" else "rpm"

    if have_power and have_hr:
        pw_bl = _band(bl, "climbing_power")
        hr_bl90 = _band(bl, "climbing_hr")
        if pw_bl and hr_bl90:
            # potencia dentro de tu rango normal (ni floja ni un pico de
            # esfuerzo) -- si la potencia tambien estuviera disparada, un
            # pulso alto seria lo esperable (mas esfuerzo = mas pulso), no
            # una señal de fatiga por si sola.
            power_normal = pw_bl["p25"] + POWER_MARGIN_W <= power < pw_bl["p75"] - POWER_MARGIN_W
            hr_spiked = hr > hr_bl90["p90"] + HR_MARGIN_BPM
            if power_normal and hr_spiked:
                wkg = f" (~{power / weight_kg:.1f} W/kg)" if weight_kg else ""
                return ("fatiga", 1, "Señal de fatiga acumulada, no de técnica", [
                    f"Potencia: {power:.0f} W{wkg} (tu habitual {pw_bl['p25']:.0f}-{pw_bl['p75']:.0f} W) -- normal para ti",
                    f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl90['p90']:.0f}) -- disparado",
                ])

    if not have_hr:
        # Tier 5 -- solo GPS+velocidad (sin pulso): comparar la velocidad del
        # tramo contra el baseline personal de velocidad en subida.
        speed_bl = _band(bl, "climbing_speed_kmh")
        speed = agg["avg_speed_kmh"]
        if speed_bl and speed is not None:
            if speed <= speed_bl["p25"] - SPEED_MARGIN_KMH:
                return ("atrancado", 5, "Más lento de lo habitual en esta pendiente", [
                    f"Velocidad: {speed:.1f} km/h (tu habitual desde {speed_bl['p25']:.1f} km/h)",
                    "Sin pulso ni marcha registrados para afinar más",
                ])
            if speed_bl["p25"] + SPEED_MARGIN_KMH < speed < speed_bl["p75"] - SPEED_MARGIN_KMH:
                return ("bien_ejecutado", 5, "Dentro de tu ritmo habitual en esta pendiente", [
                    f"Velocidad: {speed:.1f} km/h (tu habitual {speed_bl['p25']:.1f}-{speed_bl['p75']:.1f} km/h)",
                ])
        return None

    hr_bl = _band(bl, "climbing_hr")
    if not hr_bl:
        return None
    hr_high = hr >= hr_bl["p75"] + HR_MARGIN_BPM
    hr_ok = hr_bl["p25"] + HR_MARGIN_BPM <= hr <= hr_bl["p75"] - HR_MARGIN_BPM

    cad_bl = _band(bl, "climbing_cadence") if have_cadence else None
    gear_bl = _band(bl, "climbing_rear_teeth") if have_gear else None

    cad_low = cad_bl is not None and cadence <= cad_bl["p25"] - CADENCE_MARGIN
    cad_ok = cad_bl is not None and cad_bl["p25"] + CADENCE_MARGIN < cadence < cad_bl["p75"] - CADENCE_MARGIN
    gear_hard = gear_bl is not None and rear <= gear_bl["p25"] - GEAR_MARGIN_TEETH
    gear_ok = gear_bl is not None and gear_bl["p25"] + GEAR_MARGIN_TEETH < rear < gear_bl["p75"] - GEAR_MARGIN_TEETH

    # Tier 2 -- piñon + cadencia + pulso + pendiente (el caso ideal).
    if gear_bl and cad_bl:
        if gear_hard and hr_high and cad_low:
            return ("atrancado", 2, "Sube a un piñón más grande", [
                f"Piñón: ~{rear:.0f}T (tu habitual desde {gear_bl['p25']:.0f}T)",
                f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl['p75']:.0f})",
                f"Cadencia: {cadence:.0f} {cad_unit} (tu habitual desde {cad_bl['p25']:.0f})",
            ])
        if gear_ok and hr_ok and cad_ok:
            return ("bien_ejecutado", 2, "Piñón y cadencia adecuados para esta subida", [
                f"Piñón: ~{rear:.0f}T",
                f"Cadencia: {cadence:.0f} {cad_unit}",
                f"Pulso: {hr:.0f} bpm -- controlado",
            ])
        return None

    # Tier 3 -- uno de los dos (piñon o cadencia) + pulso + pendiente.
    if gear_bl and not cad_bl:
        if gear_hard and hr_high:
            return ("atrancado", 3, "Prueba un piñón más grande", [
                f"Piñón: ~{rear:.0f}T (tu habitual desde {gear_bl['p25']:.0f}T)",
                f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl['p75']:.0f})",
                "Sin datos de cadencia para afinar más",
            ])
        if gear_ok and hr_ok:
            return ("bien_ejecutado", 3, "Piñón adecuado para esta subida", [
                f"Piñón: ~{rear:.0f}T",
                f"Pulso: {hr:.0f} bpm -- controlado",
                "Sin datos de cadencia",
            ])
        return None

    if cad_bl and not gear_bl:
        if cad_low and hr_high:
            if sport == "running":
                headline = "Ritmo forzado para esta pendiente"
                details = [
                    f"Cadencia: {cadence:.0f} {cad_unit} (tu habitual desde {cad_bl['p25']:.0f})",
                    f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl['p75']:.0f})",
                ]
            else:
                headline = "Probablemente vas atrancado"
                details = [
                    f"Cadencia: {cadence:.0f} {cad_unit} (tu habitual desde {cad_bl['p25']:.0f})",
                    f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl['p75']:.0f})",
                    "Sin datos de marcha",
                ]
            return ("atrancado", 3, headline, details)
        if cad_ok and hr_ok:
            if sport == "running":
                headline = "Cadencia y pulso dentro de lo normal"
                details = [f"Cadencia: {cadence:.0f} {cad_unit}", f"Pulso: {hr:.0f} bpm"]
            else:
                headline = "Cadencia cómoda, pulso controlado"
                details = [f"Cadencia: {cadence:.0f} {cad_unit}", f"Pulso: {hr:.0f} bpm", "Sin datos de marcha"]
            return ("bien_ejecutado", 3, headline, details)
        return None

    # Tier 4 -- solo pulso + pendiente.
    extra = "Sin datos de cadencia" if sport == "running" else "Sin datos de marcha ni cadencia"
    hr_very_high = hr >= hr_bl["p90"] + HR_MARGIN_BPM
    if hr_very_high:
        return ("atrancado", 4, "Pulso muy alto para esta subida", [
            f"Pulso: {hr:.0f} bpm (tu habitual hasta {hr_bl['p90']:.0f})", extra,
        ])
    if hr_ok:
        return ("bien_ejecutado", 4, "Pulso controlado en esta subida", [f"Pulso: {hr:.0f} bpm", extra])
    return None


def _classify_descent(agg, bl, sport):
    """Patron espejo de la subida (seccion 2, 'Cobertura de bajadas'): piñon
    grande obliga a una cadencia muy alta en descenso porque no se puede
    pedalear más despacio con esa relación -- "no puedes bajar el ritmo".

    Es un patron mecanico especifico de la bici (marchas) -- en running una
    cadencia alta en bajada no es un problema (al contrario, suele ser buena
    tecnica), asi que este patron no aplica y no se evalua ningun veredicto
    de bajada para running en esta primera version (ver DISENO_ANALISIS_TRAMOS
    seccion 6 -- no hay un equivalente de bajada validado todavia)."""
    if sport != "cycling":
        return None
    cadence, rear = agg["avg_cadence"], agg["avg_rear_teeth"]
    have_cadence = cadence is not None and cadence > 0
    have_gear = rear is not None
    if not have_cadence and not have_gear:
        return None  # sin cadencia ni piñon no hay patron de bajada fiable

    cad_bl = _band(bl, "descending_cadence") if have_cadence else None
    gear_bl = _band(bl, "descending_rear_teeth") if have_gear else None

    cad_very_high = cad_bl is not None and cadence >= cad_bl["p75"] + CADENCE_MARGIN
    cad_ok = cad_bl is not None and cad_bl["p25"] + CADENCE_MARGIN < cadence < cad_bl["p75"] - CADENCE_MARGIN
    gear_easy = gear_bl is not None and rear >= gear_bl["p75"] + GEAR_MARGIN_TEETH
    gear_ok = gear_bl is not None and gear_bl["p25"] + GEAR_MARGIN_TEETH < rear < gear_bl["p75"] - GEAR_MARGIN_TEETH

    if gear_bl and cad_bl:
        if gear_easy and cad_very_high:
            return ("atrancado", 2, "Prueba un piñón más pequeño para bajar el ritmo", [
                f"Piñón: ~{rear:.0f}T (tu habitual hasta {gear_bl['p75']:.0f}T)",
                f"Cadencia: {cadence:.0f} rpm (tu habitual hasta {cad_bl['p75']:.0f}) -- muy rápida",
            ])
        if gear_ok and cad_ok:
            return ("bien_ejecutado", 2, "Piñón bien elegido para esta bajada", [
                f"Piñón: ~{rear:.0f}T", f"Cadencia: {cadence:.0f} rpm",
            ])
        return None

    if gear_bl and not cad_bl:
        if gear_easy:
            return ("atrancado", 3, "Probablemente vas pedaleando más rápido de lo cómodo", [
                f"Piñón: ~{rear:.0f}T (tu habitual hasta {gear_bl['p75']:.0f}T)",
                "Sin datos de cadencia",
            ])
        if gear_ok:
            return ("bien_ejecutado", 3, "Piñón razonable para esta bajada", [
                f"Piñón: ~{rear:.0f}T", "Sin datos de cadencia",
            ])
        return None

    if cad_bl and not gear_bl:
        if cad_very_high:
            return ("atrancado", 3, "Prueba un piñón más pequeño", [
                f"Cadencia: {cadence:.0f} rpm (tu habitual hasta {cad_bl['p75']:.0f}) -- muy alta",
                "Sin datos de marcha",
            ])
        if cad_ok:
            return ("bien_ejecutado", 3, "Cadencia cómoda para esta bajada", [
                f"Cadencia: {cadence:.0f} rpm", "Sin datos de marcha",
            ])
        return None

    return None


def classify_tramo(agg, bl, sport="cycling", weight_kg=None):
    """Devuelve (verdict, reliability_tier, reason_headline, reason_details,
    direction) o None si el tramo es llano (sin veredicto) o no hay señal
    suficiente para uno claro. reason_headline es una frase corta con la
    conclusion; reason_details es una lista de comparaciones contra tu
    historial personal (para pintar como bullets en la interfaz, en vez de
    una unica frase larga que mezcla conclusion y datos)."""
    grade = agg["avg_grade_pct"]
    if grade is None:
        return None

    climbing_thr = bl.get("climbing_grade_threshold_pct", 3.0)
    descending_thr = bl.get("descending_grade_threshold_pct", -3.0)

    if grade >= climbing_thr:
        result = _classify_climb(agg, bl, sport, weight_kg)
        direction = "climb"
    elif grade <= descending_thr:
        result = _classify_descent(agg, bl, sport)
        direction = "descent"
    else:
        return None  # tramo llano: no aplica ni "atrancado" ni "bien ejecutado"

    if result is None:
        return None
    verdict, tier, headline, details = result
    return verdict, tier, headline, details, direction


def find_interesting_points(records, baselines, sport="cycling", weight_kg=None, min_duration_s=MIN_DURATION_S):
    """Segmenta la actividad en tramos y clasifica cada uno; devuelve la
    lista de tramos con veredicto claro (atrancado / bien_ejecutado / fatiga),
    descartando los llanos y los que se quedan sin señal suficiente."""
    tramos = segment_tramos(records)
    episodes = []
    # Momento de la ruta en el que ocurre el tramo (no cuanto se lleva
    # subiendo/pedaleando, sino desde el inicio de la actividad entera) --
    # una rampa a los 10 min con las piernas frias no es comparable a la misma
    # rampa a las 4h con las piernas cansadas, aunque el pulso salga igual de
    # "alto" en las dos. De momento solo se expone el dato (ver
    # DISENO_ANALISIS_TRAMOS): no se usa todavia para ajustar el baseline,
    # el usuario lo interpreta el mismo con el contexto delante.
    ride_start_ts = records[0]["ts"] if records else None
    for idx0, idx1 in tramos:
        agg = summarize_tramo(records, idx0, idx1)
        if agg["duration_s"] < min_duration_s:
            continue
        classified = classify_tramo(agg, baselines, sport, weight_kg)
        if classified is None:
            continue
        verdict, tier, headline, details, direction = classified
        episode = dict(agg)
        episode.update({
            "verdict": verdict,
            "reliability_tier": tier,
            "reason_headline": headline,
            "reason_details": details,
            "direction": direction,
            "elapsed_since_start_s": (agg["start_ts"] - ride_start_ts) if ride_start_ts is not None else None,
        })
        episodes.append(episode)
    return episodes


# Selección mínima para que un tramo elegido a mano tenga algo de señal --
# por debajo de esto es mas ruido de un arrastre corto en la grafica que una
# seleccion deliberada.
MIN_CUSTOM_RANGE_S = 5


def analyze_custom_range(records, start_distance_m, end_distance_m, baselines, sport, weight_kg=None):
    """Analiza un tramo elegido a mano por el usuario (arrastrando sobre la
    grafica del detalle de una actividad), con el mismo motor que los tramos
    automaticos -- misma agregacion (summarize_tramo), mismo baseline, mismo
    veredicto (classify_tramo). No pasa por segment_tramos(): esa
    segmentacion sirve para DETECTAR tramos solos; aqui el usuario ya ha
    decidido el rango exacto, no hace falta adivinarlo.

    A diferencia de un tramo automatico, si la seleccion es llana o mixta
    (sin pendiente sostenida de subida/bajada) no se descarta -- se devuelve
    igualmente con veredicto None, para que el usuario vea al menos el
    resumen y el historial de "otras veces por aqui" de esa seleccion,
    aunque no aplique el mismo diagnostico de atrancado/bien_ejecutado."""
    idxs = [i for i, r in enumerate(records)
            if r.get("distance") is not None and start_distance_m <= r["distance"] <= end_distance_m]
    if len(idxs) < 2:
        return {"error": "Selección demasiado corta para analizar."}

    idx0, idx1 = idxs[0], idxs[-1]
    agg = summarize_tramo(records, idx0, idx1)
    if agg["duration_s"] < MIN_CUSTOM_RANGE_S:
        return {"error": "Selección demasiado corta para analizar."}

    ride_start_ts = records[0]["ts"] if records else None
    episode = dict(agg)
    classified = classify_tramo(agg, baselines, sport, weight_kg)
    if classified:
        verdict, tier, headline, details, direction = classified
    else:
        verdict, tier, direction = None, None, None
        grade = agg["avg_grade_pct"]
        climbing_thr = baselines.get("climbing_grade_threshold_pct", 3.0)
        descending_thr = baselines.get("descending_grade_threshold_pct", -3.0)
        # Ojo: que classify_tramo no de veredicto NO significa siempre "pendiente
        # floja" -- classify_tramo primero mira si la pendiente ya cruza el
        # umbral personal de subida/bajada, y solo en ese caso entra en
        # _classify_climb/_classify_descent a intentar un veredicto por tier
        # (potencia/marcha/cadencia/pulso/velocidad); si esa pendiente SI cruza
        # el umbral pero ninguna señal alcanza (p.ej. sin pulso ni marcha ni
        # cadencia en esta seleccion concreta, o pocas muestras en el
        # baseline), tambien vuelve None -- y sería enganoso decir "pendiente
        # floja" para una subida real (visto con datos reales: 11.6% de media,
        # muy por encima del umbral de 2.6%, etiquetada "subida suave" por no
        # distinguir estos dos casos).
        if grade is None:
            headline = "Sin datos de pendiente suficientes en esta selección"
            details = []
        elif grade >= climbing_thr or grade <= descending_thr:
            tendencia = "subida" if grade >= climbing_thr else "bajada"
            headline = f"Es una {tendencia} real para ti, pero sin datos suficientes para un diagnóstico"
            details = [f"Pendiente: {grade:.1f}% (por encima de tu umbral habitual de {tendencia})",
                       "Sin suficientes datos de pulso/cadencia/marcha/velocidad en esta selección"]
        elif -0.5 <= grade <= 0.5:
            headline = "Tramo llano -- no aplica un veredicto de atrancado/bien ejecutado"
            details = []
        else:
            tendencia, umbral = ("subida", climbing_thr) if grade > 0 else ("bajada", descending_thr)
            headline = f"{tendencia.capitalize()} suave, por debajo de tu umbral habitual"
            details = [f"Pendiente: {grade:.1f}% (tu umbral de {tendencia}: {umbral:.1f}%)"]
    episode.update({
        "verdict": verdict,
        "reliability_tier": tier,
        "reason_headline": headline,
        "reason_details": details,
        "direction": direction,
        "elapsed_since_start_s": (agg["start_ts"] - ride_start_ts) if ride_start_ts is not None else None,
        "custom": True,
    })
    return episode


def main():
    # baselines.json tiene un arbol independiente por deporte (ver
    # baselines.py) -- mezclar cadencia de ciclismo (rpm) con la de running
    # (spm) en un mismo baseline no tendria sentido.
    baselines_by_sport = load_baselines()
    profile = profile_store.load_profile()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    total_episodes = 0
    by_verdict = {"atrancado": 0, "bien_ejecutado": 0, "fatiga": 0}
    by_tier = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    rides_with_episodes = 0

    seen_ids = set()
    for path in sorted(RECORDS_DIR.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        records = d["records"]
        activity_id = d["activity_id"]
        seen_ids.add(activity_id)
        sport = d.get("sport", "cycling")
        baselines = baselines_by_sport.get(sport)
        if not baselines:
            # nunca deberia pasar (main() de baselines.py genera un arbol por
            # cada deporte presente en los records), pero por si acaso: sin
            # baseline no hay percentiles con los que comparar, se salta.
            continue

        ride_date = datetime.fromtimestamp(records[0]["ts"]).date().isoformat() if records else None
        weight_kg = profile_store.weight_at_date(ride_date, profile) if ride_date else None
        episodes = find_interesting_points(records, baselines, sport, weight_kg)
        if episodes:
            rides_with_episodes += 1
        total_episodes += len(episodes)
        for e in episodes:
            by_verdict[e["verdict"]] += 1
            by_tier[e["reliability_tier"]] += 1

        out_path = OUT_DIR / f"{activity_id}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"activity_id": activity_id, "episodes": episodes}, f,
                       indent=2, ensure_ascii=False)

    # Huerfanos: un records.json.gz borrado (p.ej. por deduplicacion de
    # Strava, ver strava_sync.py) deja atras su .json de tramos aqui si no se
    # limpia -- nunca se sirven (la app carga siempre por records primero),
    # pero se acumulan sin razon y pueden confundir a quien mire data/ a
    # mano. Se borra cualquier fichero que ya no tenga un records.json.gz.
    if OUT_DIR.exists():
        n_orphans = 0
        for f in OUT_DIR.glob("*.json"):
            if f.stem not in seen_ids:
                f.unlink()
                n_orphans += 1
        if n_orphans:
            print(f"Huerfanos eliminados (sin records.json.gz): {n_orphans}")

    print(f"Tramos interesantes totales: {total_episodes}")
    print(f"  atrancado:      {by_verdict['atrancado']}")
    print(f"  bien ejecutado: {by_verdict['bien_ejecutado']}")
    print(f"  fatiga:         {by_verdict['fatiga']}")
    print("Por tier de fiabilidad:")
    for t in (1, 2, 3, 4, 5):
        print(f"  tier {t}: {by_tier[t]}")
    print(f"Actividades con al menos un tramo interesante: {rides_with_episodes}")


if __name__ == "__main__":
    main()
