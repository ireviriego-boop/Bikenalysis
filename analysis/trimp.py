#!/usr/bin/env python3
"""
Carga de entrenamiento basada solo en pulso (TRIMP, formula de Banister con
reserva de pulso) y el modelo Fitness/Fatiga/Forma (Banister Fitness-Fatigue,
lo que TrainingPeaks/Strava llaman CTL/ATL/TSB) -- ver DISENO_ANALISIS_TRAMOS
seccion 5. Al usar solo pulso vale para cualquier deporte por igual: se suma
la misma serie diaria de carga sin importar si el dia fue en bici o
corriendo.

Genera data/fitness.json con una fila por dia (desde la primera actividad
hasta hoy, sin huecos) para la grafica y para poder anotar cualquier
actividad pasada con la carga acumulada de esos dias (seccion 5, "la fatiga
acumulada como contexto").
"""

import gzip
import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import profile_store

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BIKENALYSIS_DATA_DIR", HERE / "data"))
RECORDS_DIR = DATA_DIR / "records"
SUMMARIES_FILE = DATA_DIR / "summaries.json"
FITNESS_FILE = DATA_DIR / "fitness.json"

# Constantes de tiempo del modelo Fitness-Fatigue de Banister: "Fitness" es
# un acumulador lento (semanas) y "Fatiga" uno rapido (dias) -- ver seccion 5.
TAU_FITNESS_DAYS = 42.0
TAU_FATIGA_DAYS = 7.0

# Formula de Banister del TRIMP individualizado (ponderacion exponencial por
# reserva de pulso). Los coeficientes difieren un poco entre hombre/mujer
# pero solo cambian la escala, no la forma de la curva -- de momento se usa
# siempre la constante "hombre" (0.64 / 1.92); es una aproximacion razonable
# para una serie de carga relativa, no una medida clinica.
TRIMP_K = {"M": 0.64, "F": 0.86}
TRIMP_B = {"M": 1.92, "F": 1.67}

# Huecos de mas de esto entre dos muestras de pulso consecutivas (pausas,
# perdida de GPS/sensor) no cuentan como tiempo de esfuerzo continuo.
MAX_GAP_MIN = 5.0

FALLBACK_MAX_HR = 190.0


def _local_date(ts):
    return datetime.fromtimestamp(ts).date()


def _load_summaries():
    if not SUMMARIES_FILE.exists():
        return []
    with open(SUMMARIES_FILE, encoding="utf-8") as f:
        return json.load(f).get("summaries", [])


def _load_records(activity_id):
    path = RECORDS_DIR / f"{activity_id}.json.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)["records"]


def infer_max_hr(summaries, age=None):
    """Pulso maximo inferido del historico (DISENO_ANALISIS_TRAMOS seccion
    8: a diferencia del reposo, el maximo si se puede inferir de los datos).
    Se usa el percentil 99 en vez del maximo absoluto: con miles de
    actividades (aqui, varias de Strava desde 2017) el maximo literal cae
    facilmente en un pico puntual del sensor (ej. 225 bpm sostenido no es
    creible) que distorsionaria la reserva de pulso de TODO el TRIMP.

    Si no hay NINGUNA actividad con pulso maximo registrado (usuario muy
    nuevo, o sin sensor de pulso todavia), se usa una formula generica por
    edad (Tanaka et al. 2001, 208 - 0.7*edad -- mas fiable que la clasica
    220-edad) si el usuario la ha dado; si no, un valor fijo de respaldo.
    Es solo un arranque en frio -- en cuanto haya una sola actividad con
    pulso real, esta rama deja de usarse."""
    vals = sorted(s["max_hr"] for s in summaries if s.get("max_hr"))
    if not vals:
        if age:
            return 208.0 - 0.7 * age
        return FALLBACK_MAX_HR
    if len(vals) < 20:
        return vals[-1]
    idx = min(len(vals) - 1, int(len(vals) * 0.99))
    return vals[idx]


def _activity_trimp(records, resting_hr, max_hr, sex):
    hrr_range = max_hr - resting_hr
    if hrr_range <= 0:
        return 0.0
    k, b = TRIMP_K.get(sex, TRIMP_K["M"]), TRIMP_B.get(sex, TRIMP_B["M"])
    total = 0.0
    prev = None
    for r in records:
        hr = r.get("heart_rate")
        ts = r.get("ts")
        if hr is None or ts is None:
            prev = None
            continue
        if prev is not None:
            dt_min = (ts - prev[0]) / 60.0
            if 0 < dt_min <= MAX_GAP_MIN:
                avg_hr = (hr + prev[1]) / 2.0
                frac = max(0.0, min(1.0, (avg_hr - resting_hr) / hrr_range))
                total += dt_min * frac * (k * math.exp(b * frac))
        prev = (ts, hr)
    return total


def _daily_trimp(summaries, resting_hr, max_hr, sex):
    """TRIMP diario sumando todas las actividades (de cualquier deporte) de
    cada dia -- {date: trimp}."""
    daily = {}
    for s in summaries:
        if not s.get("start_ts"):
            continue
        records = _load_records(s["activity_id"])
        if not records:
            continue
        d = _local_date(s["start_ts"])
        daily[d] = daily.get(d, 0.0) + _activity_trimp(records, resting_hr, max_hr, sex)
    return daily


def _label_forma(forma, fitness):
    """Etiqueta cualitativa de 'Forma' (ver DISENO_ANALISIS_TRAMOS seccion
    5): se expresa como fraccion de la propia Fitness del usuario en vez de
    con umbrales absolutos, para que la banda tenga sentido sea cual sea el
    volumen de entrenamiento habitual de cada persona."""
    ratio = (forma / fitness) if fitness > 1e-6 else 0.0
    if ratio >= 0.25:
        return "muy fresco"
    if ratio >= 0.05:
        return "fresco"
    if ratio >= -0.10:
        return "normal"
    if ratio >= -0.30:
        return "cansado"
    return "muy cansado"


def _project_recovery(fitness, fatiga, max_days=14):
    """Si a partir de hoy no se entrena mas (TRIMP=0 cada dia), cuantos dias
    hasta que la Forma vuelva a la banda 'normal' o mejor -- para poder
    sugerir un dia razonable para el proximo esfuerzo fuerte. Es solo una
    proyeccion con pulso (avisos de DISENO_ANALISIS_TRAMOS seccion 5: no
    tiene en cuenta sueno, enfermedad, calor...), asi que se presenta como
    guia aproximada, no como prescripcion."""
    k_fitness = 1 - math.exp(-1 / TAU_FITNESS_DAYS)
    k_fatiga = 1 - math.exp(-1 / TAU_FATIGA_DAYS)
    f, a = fitness, fatiga
    for i in range(1, max_days + 1):
        f = f + (0.0 - f) * k_fitness
        a = a + (0.0 - a) * k_fatiga
        entrance_forma = f - a
        ratio = (entrance_forma / f) if f > 1e-6 else 0.0
        if ratio >= -0.10:
            return i
    return None


def _recommendation(forma, fitness, fatiga):
    """Traduce la Forma actual en una sugerencia en lenguaje llano de si hoy
    es buen dia para un esfuerzo fuerte, uno suave, o mejor descansar --
    y si toca descansar, en cuantos dias (asumiendo descanso) se recuperaria
    la Forma. Ver aviso de fiabilidad en _project_recovery."""
    label = _label_forma(forma, fitness)
    if label in ("muy fresco", "fresco"):
        return {"label": label, "message": "Buen momento para un esfuerzo fuerte."}
    if label == "normal":
        return {"label": label, "message": "Puedes entrenar con normalidad."}

    days = _project_recovery(fitness, fatiga)
    if days is None:
        eta = "seguramente necesites más de 2 semanas de descanso relativo"
    elif days == 1:
        eta = "si hoy es suave/descanso, mañana deberías notar mejoría"
    else:
        eta = f"si no metes esfuerzos fuertes, en torno a {days} días deberías estar recuperado"

    if label == "cansado":
        return {"label": label, "message": f"Carga acumulada alta -- mejor hoy suave o descanso ({eta})."}
    return {"label": label, "message": f"Carga acumulada muy alta -- descanso recomendado ({eta})."}


def compute_fitness_series():
    """Devuelve (y guarda en data/fitness.json) la serie diaria completa
    desde la primera actividad hasta hoy: trimp del dia, Fitness (lento),
    Fatiga (rapido) y Forma (Fitness menos Fatiga del dia anterior, es decir
    'como llegabas' a ese dia -- convencion habitual de CTL/ATL/TSB)."""
    profile = profile_store.load_profile()
    summaries = _load_summaries()
    resting_hr = profile.get("resting_hr")

    if resting_hr is None or not summaries:
        result = {"configured": resting_hr is not None, "has_data": bool(summaries), "days": []}
        FITNESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FITNESS_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        return result

    max_hr = infer_max_hr(summaries, profile.get("age"))
    sex = profile.get("sex", "M")
    daily = _daily_trimp(summaries, resting_hr, max_hr, sex)

    start = min(daily) if daily else date.today()
    today = date.today()

    k_fitness = 1 - math.exp(-1 / TAU_FITNESS_DAYS)
    k_fatiga = 1 - math.exp(-1 / TAU_FATIGA_DAYS)

    days = []
    fitness = 0.0
    fatiga = 0.0
    d = start
    while d <= today:
        trimp = daily.get(d, 0.0)
        forma = fitness - fatiga  # como se llegaba a este dia, antes de entrenar hoy
        fitness = fitness + (trimp - fitness) * k_fitness
        fatiga = fatiga + (trimp - fatiga) * k_fatiga
        days.append({
            "date": d.isoformat(),
            "trimp": round(trimp, 1),
            "fitness": round(fitness, 1),
            "fatiga": round(fatiga, 1),
            "forma": round(forma, 1),
        })
        d += timedelta(days=1)

    # La 'forma de hoy' util para mostrar es la de manana (con el
    # entrenamiento de hoy ya contabilizado) -- se añade una fila extra
    # "virtual" no es necesario: el ultimo dia real ya tiene su Forma de
    # entrada calculada arriba; para el indicador principal se usa
    # fitness/fatiga de HOY (tras el entrenamiento de hoy) menos, ver abajo.
    result = {
        "configured": True,
        "has_data": True,
        "resting_hr": resting_hr,
        "max_hr": max_hr,
        "days": days,
    }
    FITNESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FITNESS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return result


def load_fitness_summary(recent_days=14):
    """Para la API: estado actual (Forma/Fitness/Fatiga de hoy, con
    etiqueta) + los ultimos `recent_days` para el listado 'ultimos dias y su
    efecto', ademas de toda la serie para la grafica."""
    if not FITNESS_FILE.exists():
        compute_fitness_series()
    with open(FITNESS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    if not data.get("configured") or not data.get("days"):
        return data

    days = data["days"]
    last = days[-1]
    # Fitness/Fatiga de HOY ya incluyen el entrenamiento de hoy (si lo hubo);
    # la Forma "actual" mas representativa de cara a manana es esa resta de
    # hoy, no la de entrada de hoy (que es la que se guarda por dia para
    # poder anotar actividades pasadas).
    forma_now = round(last["fitness"] - last["fatiga"], 1)
    data["current"] = {
        "date": last["date"],
        "fitness": last["fitness"],
        "fatiga": last["fatiga"],
        "forma": forma_now,
        "forma_label": _label_forma(forma_now, last["fitness"]),
        "recommendation": _recommendation(forma_now, last["fitness"], last["fatiga"]),
    }
    data["recent"] = days[-recent_days:]
    return data


def forma_context_for_date(iso_date):
    """Para anotar una actividad concreta con la carga acumulada de esos
    dias (seccion 5, 'la fatiga acumulada como contexto') -- NO cambia
    ningun veredicto, solo da el dato con el que interpretarlo, siguiendo lo
    que pidio el usuario ('mostrar el dato, no cambiar el veredicto')."""
    if not FITNESS_FILE.exists():
        return None
    with open(FITNESS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("configured"):
        return None
    for row in data.get("days", []):
        if row["date"] == iso_date:
            return {
                "forma": row["forma"],
                "forma_label": _label_forma(row["forma"], row["fitness"]),
                "fatiga": row["fatiga"],
            }
    return None


def main():
    result = compute_fitness_series()
    if not result.get("configured"):
        print("Perfil fisico sin pulso en reposo configurado -- no se puede calcular el TRIMP todavia.")
        return
    n = len(result["days"])
    print(f"Fitness/Fatiga/Forma recalculado: {n} dias (pulso reposo {result['resting_hr']}, "
          f"pulso max inferido {result['max_hr']:.0f}).")
    if n:
        last = result["days"][-1]
        print(f"Hoy ({last['date']}): fitness={last['fitness']}, fatiga={last['fatiga']}, "
              f"forma de entrada={last['forma']}")


if __name__ == "__main__":
    main()
