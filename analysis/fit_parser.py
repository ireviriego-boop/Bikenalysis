"""
Parser minimo de FIT (sin dependencias externas) para el proyecto Bikenalysis.

Extrae, de un fichero .fit de Karoo/Ki2 (ciclismo) o de reloj Amazfit/Zepp
(running -- ver DISENO_ANALISIS_TRAMOS seccion 6):
  - 'sport': 'cycling' o 'running' (leido del mensaje 'sport' del FIT).
  - 'records': lista de puntos (timestamp unix, lat, lon, altitude m,
    distance m, speed m/s, heart_rate bpm, cadence rpm/spm, grade %,
    temperature C, power W si hay) con el estado de marcha (plato/pinon,
    dientes e indice) ya fusionado en cada punto -- solo aplica a ciclismo,
    running nunca trae estos eventos.
  - 'gear_events': lista cruda de cambios de marcha tal como vienen en el FIT.
  - 'n_front_shifts' / 'n_rear_shifts': conteos.

En running, a diferencia de ciclismo: la altitud viene en el campo
'enhanced_altitude' (78) en vez de 'altitude' (2); la cadencia (campo 4) viene
en zancadas/min de una sola pierna y hay que doblarla para pasos/min reales;
y ni la distancia ni la pendiente vienen dadas por el dispositivo, hay que
derivarlas (ver _derive_distance_and_grade).

FIT epoch: 1989-12-31T00:00:00Z = unix 631065600
"""

import math
import struct
import collections
from pathlib import Path

FIT_EPOCH_OFFSET = 631065600

BASE_TYPES = {
    0x00: ('enum', 1, 'B', 0xFF),
    0x01: ('sint8', 1, 'b', 0x7F),
    0x02: ('uint8', 1, 'B', 0xFF),
    0x83: ('sint16', 2, 'h', 0x7FFF),
    0x84: ('uint16', 2, 'H', 0xFFFF),
    0x85: ('sint32', 4, 'i', 0x7FFFFFFF),
    0x86: ('uint32', 4, 'I', 0xFFFFFFFF),
    0x07: ('string', 1, 's', None),
    0x88: ('float32', 4, 'f', None),
    0x89: ('float64', 8, 'd', None),
    0x0A: ('uint8z', 1, 'B', 0x00),
    0x8B: ('uint16z', 2, 'H', 0x0000),
    0x8C: ('uint32z', 4, 'I', 0x00000000),
    0x0D: ('byte', 1, 'B', 0xFF),
    0x8E: ('sint64', 8, 'q', None),
    0x8F: ('uint64', 8, 'Q', None),
    0x90: ('uint64z', 8, 'Q', None),
}


def _decode_field(raw_bytes, base_type_byte, size, endian='<'):
    """Decodifica un campo. `endian` debe ser el de la Definition Message a la que
    pertenece el campo ('<' o '>'), NO siempre little-endian -- el byte 'arch' de
    cada definicion puede ser big-endian (visto en los ficheros de este Karoo), y
    si se ignora esto los campos multi-byte (timestamp, distancia, lat/lon, etc.)
    salen con valores sin sentido aunque el desplazamiento de bytes sea correcto.
    """
    info = BASE_TYPES.get(base_type_byte)
    if info is None:
        return None
    name, unit_size, fmt, invalid = info
    if name == 'string':
        s = raw_bytes.split(b'\x00')[0]
        try:
            return s.decode('utf-8', errors='replace')
        except Exception:
            return None
    count = max(1, size // unit_size)
    vals = []
    for i in range(count):
        chunk = raw_bytes[i * unit_size:(i + 1) * unit_size]
        if len(chunk) < unit_size:
            break
        v = struct.unpack(endian + fmt, chunk)[0]
        if invalid is not None and v == invalid:
            continue
        vals.append(v)
    if not vals:
        return None
    return vals[0] if len(vals) == 1 else vals


def _iter_messages(data):
    """Generador de bajo nivel: (global_mesg_num, values_dict) por cada mensaje de datos.

    Importante: maneja la "Compressed Timestamp Header" del formato FIT. Cuando el
    bit 0x80 esta activo, el mensaje NO lleva el campo timestamp (253) en el propio
    payload -- el timestamp se deriva de un offset de 5 bits sobre el ultimo
    timestamp absoluto visto. Si no se maneja esto, la lectura de TODOS los campos
    siguientes de ese mensaje (y de ahi en adelante) queda desalineada, produciendo
    valores basura (distancia/lat/lon/velocidad sin sentido) -- exactamente el bug
    que tenian las primeras versiones de este parser.
    """
    header_size = data[0]
    data_size = struct.unpack('<I', data[4:8])[0]
    pos = header_size
    end = header_size + data_size
    local_defs = {}
    last_timestamp = None  # ultimo timestamp absoluto (unidades FIT, sin offset de epoca)

    while pos < end:
        header = data[pos]
        pos += 1
        compressed = bool(header & 0x80)

        if compressed:
            local_type = (header >> 5) & 0x3
            time_offset = header & 0x1F
            if local_type not in local_defs:
                break
            global_mesg_num, fields, dev_fields, endian = local_defs[local_type]

            base_ts = last_timestamp if last_timestamp is not None else 0
            new_ts = (base_ts & ~0x1F) + time_offset
            if new_ts < base_ts:
                new_ts += 0x20
            last_timestamp = new_ts

            values = {}
            raw_bytes = {}
            for fnum, fsize, ftype in fields:
                if fnum == 253:
                    # el timestamp no viene en el payload en modo comprimido
                    values[253] = last_timestamp
                    continue
                raw = data[pos:pos + fsize]
                pos += fsize
                values[fnum] = _decode_field(raw, ftype, fsize, endian)
                raw_bytes[fnum] = raw
            for fnum, fsize, dev_idx in dev_fields:
                pos += fsize
            values['_raw'] = raw_bytes
            yield global_mesg_num, values
            continue

        is_definition = bool(header & 0x40)
        local_type = header & 0xF

        if is_definition:
            pos += 1  # reserved
            arch = data[pos]
            pos += 1
            # El byte 'arch' fija el orden de bytes de TODOS los campos de esta
            # definicion (no solo el numero de mensaje global) -- en los FIT de
            # este Karoo se ha visto arch=1 (big-endian) para el mensaje 'record'.
            endian = '<' if arch == 0 else '>'
            global_mesg_num = struct.unpack(endian + 'H', data[pos:pos + 2])[0]
            pos += 2
            num_fields = data[pos]
            pos += 1
            fields = []
            for _ in range(num_fields):
                fnum, fsize, ftype = data[pos], data[pos + 1], data[pos + 2]
                pos += 3
                fields.append((fnum, fsize, ftype))
            dev_fields = []
            if header & 0x20:
                num_dev = data[pos]
                pos += 1
                for _ in range(num_dev):
                    fnum, fsize, dev_idx = data[pos], data[pos + 1], data[pos + 2]
                    pos += 3
                    dev_fields.append((fnum, fsize, dev_idx))
            local_defs[local_type] = (global_mesg_num, fields, dev_fields, endian)
        else:
            if local_type not in local_defs:
                break
            global_mesg_num, fields, dev_fields, endian = local_defs[local_type]
            values = {}
            raw_bytes = {}
            for fnum, fsize, ftype in fields:
                raw = data[pos:pos + fsize]
                pos += fsize
                v = _decode_field(raw, ftype, fsize, endian)
                values[fnum] = v
                raw_bytes[fnum] = raw
                if fnum == 253 and v is not None:
                    last_timestamp = v
            for fnum, fsize, dev_idx in dev_fields:
                pos += fsize  # ignoramos campos de desarrollador aqui
            values['_raw'] = raw_bytes
            yield global_mesg_num, values


SPORT_ENUM = {0: 'generic', 1: 'running', 2: 'cycling', 5: 'swimming'}


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _derive_distance_and_grade(records, grade_window=5):
    """Para deportes sin distancia/pendiente dadas en el FIT (running, ver
    DISENO_ANALISIS_TRAMOS seccion 6): deriva la distancia acumulada a partir
    de las coordenadas GPS (haversine punto a punto -- mas robusto que
    integrar la velocidad, que puede estar mal calibrada) y la pendiente de
    una ventana de +/-grade_window puntos de distancia+altitud -- un diff
    punto a punto seria puro ruido de GPS/altimetro a 1Hz."""
    dist = 0.0
    last_lat = last_lon = None
    for r in records:
        lat, lon = r.get('lat'), r.get('lon')
        if lat is not None and lon is not None:
            if last_lat is not None:
                dist += _haversine_m(last_lat, last_lon, lat, lon)
            last_lat, last_lon = lat, lon
        r['distance'] = dist

    n = len(records)
    for i in range(n):
        i0 = max(0, i - grade_window)
        i1 = min(n - 1, i + grade_window)
        d0, d1 = records[i0].get('distance'), records[i1].get('distance')
        a0, a1 = records[i0].get('altitude'), records[i1].get('altitude')
        if d0 is None or d1 is None or a0 is None or a1 is None:
            continue
        dd = d1 - d0
        if dd < 1.0:  # ventana casi sin desplazamiento (parado): pendiente indefinida
            continue
        records[i]['grade'] = (a1 - a0) / dd * 100.0


def parse_activity(path):
    data = Path(path).read_bytes()
    if data[8:12] != b'.FIT':
        raise ValueError(f"{path}: no parece un fichero FIT valido")

    raw_values = []  # dicts de campos crudos (sin convertir) del mensaje 'record'
    gear_events = []
    n_front_shifts = 0
    n_rear_shifts = 0
    # 'cycling' por defecto: todos los FIT vistos hasta ahora sin mensaje
    # 'sport' explicito eran del Karoo (bici). El mensaje 'sport' puede
    # aparecer en cualquier punto del fichero (algunos encoders lo escriben
    # al final junto al resumen), asi que hace falta un primer barrido
    # completo antes de saber que deporte es -- de ahi que 'record' solo
    # guarde los valores crudos aqui y la conversion a unidades fisicas
    # (que depende del deporte, ver mas abajo) se haga en una segunda pasada.
    sport = 'cycling'
    sport_enum = None

    for gm, values in _iter_messages(data):
        if gm == 12:  # sport -- trae el deporte como texto (campo 3, 'name'
            # en el perfil oficial FIT) ademas del enum (campo 0); se prefiere
            # el texto cuando esta, verificado con datos reales del Amazfit
            # Balance (campo 3 = "running" literal).
            name = values.get(3)
            if isinstance(name, str) and name.strip():
                sport = name.strip().lower()
            elif values.get(0) is not None:
                sport_enum = values[0]
        elif gm == 20:  # record
            if values.get(253) is not None:
                raw_values.append(values)
        elif gm == 21:  # event
            ev = values.get(0)
            ts = values.get(253)
            raw3 = values.get('_raw', {}).get(3)
            # El campo 'data' (def_num 3) de front/rear_gear_change es en realidad
            # 4 bytes empaquetados (front_teeth, front_index, rear_teeth,
            # rear_index) en el orden en que vienen en el fichero -- NO es un
            # entero escalar sujeto al 'arch' (endianness) de la definicion.
            # Verificado con datos reales: interpretando el campo asi, front_teeth
            # se queda constante (~34, un solo plato) mientras rear_teeth sube y
            # baja de forma monotona y coherente con rear_index durante cambios
            # de pinon reales; aplicar el endian de la definicion (big-endian en
            # estos ficheros) da valores fisicamente imposibles (p.ej. platos de
            # 5 dientes).
            if ev in (42, 43) and raw3 is not None and len(raw3) == 4 and ts is not None:
                front_teeth = raw3[0]
                front_index = raw3[1]
                rear_teeth = raw3[2]
                rear_index = raw3[3]
                gear_events.append({
                    'ts': ts + FIT_EPOCH_OFFSET,
                    'kind': 'front' if ev == 42 else 'rear',
                    'front_teeth': front_teeth,
                    'front_index': front_index,
                    'rear_teeth': rear_teeth,
                    'rear_index': rear_index,
                })
                if ev == 42:
                    n_front_shifts += 1
                else:
                    n_rear_shifts += 1

    gear_events.sort(key=lambda e: e['ts'])

    if sport == 'cycling' and sport_enum is not None:
        sport = SPORT_ENUM.get(sport_enum, 'cycling')

    # segunda pasada: ahora que se conoce el deporte, se convierten los
    # valores crudos del mensaje 'record' a unidades fisicas -- el mapeo de
    # campos difiere entre ciclismo y running (ver cabecera del fichero).
    raw_records = []
    for values in raw_values:
        rec = {'ts': values[253] + FIT_EPOCH_OFFSET}
        if values.get(0) is not None and values.get(1) is not None:
            rec['lat'] = values[0] * (180.0 / 2**31)
            rec['lon'] = values[1] * (180.0 / 2**31)
        # altitud: 'enhanced_altitude' (78) tiene preferencia sobre 'altitude'
        # (2) cuando esta -- es el unico que trae running, y en ciclismo
        # ambos coinciden cuando los dos existen.
        alt_raw = values.get(78, values.get(2))
        if alt_raw is not None:
            rec['altitude'] = alt_raw / 5.0 - 500.0
        if values.get(3) is not None:
            rec['heart_rate'] = values[3]
        if sport == 'running':
            if values.get(4) is not None:
                # cadencia de zancada (campo 4, entero) + fraccion (campo 53,
                # escala 128) -- de UNA pierna, se dobla para pasos/min reales.
                # Verificado con datos reales del Amazfit Balance: sin doblar
                # salian ~55-100 spm (paso de maraton lento imposible), con la
                # fisica real velocidad = (cadencia*2/60) * long. zancada
                # cuadra con margen de error pequeño.
                frac = (values.get(53) or 0) / 128.0
                rec['cadence'] = (values[4] + frac) * 2.0
            if values.get(85) is not None:
                # longitud de zancada: viene en decimas de mm -> metros.
                rec['step_length'] = values[85] / 10000.0
        else:
            if values.get(4) is not None:
                rec['cadence'] = values[4]
        if values.get(5) is not None:
            rec['distance'] = values[5] / 100.0
        if values.get(6) is not None:
            rec['speed'] = values[6] / 1000.0
        if values.get(7) is not None:
            # potencia (vatios). En bici, ningun dispositivo del usuario la
            # graba todavia (ver DISENO_ANALISIS_TRAMOS seccion 2); en running
            # el propio reloj SI la calcula y la graba (confirmado con datos
            # reales), aunque siga siendo una estimacion, no una fuerza
            # medida -- se trata igual en ambos casos aqui, es el consumidor
            # (baselines/clasificador) quien decide como de fiable es cada una.
            rec['power'] = values[7]
        if values.get(9) is not None:
            rec['grade'] = values[9] / 100.0
        if values.get(13) is not None:
            rec['temperature'] = values[13]
        raw_records.append(rec)

    if sport == 'running':
        _derive_distance_and_grade(raw_records)

    # fusionar el estado de marcha en cada punto (forward-fill por timestamp)
    # -- solo aplica de verdad a ciclismo, running nunca trae estos eventos.
    records = []
    gi = 0
    current = None
    for rec in raw_records:
        while gi < len(gear_events) and gear_events[gi]['ts'] <= rec['ts']:
            current = gear_events[gi]
            gi += 1
        merged = dict(rec)
        if current is not None:
            merged['front_teeth'] = current['front_teeth']
            merged['front_index'] = current['front_index']
            merged['rear_teeth'] = current['rear_teeth']
            merged['rear_index'] = current['rear_index']
        records.append(merged)

    return {
        'path': str(path),
        'sport': sport,
        'records': records,
        'gear_events': gear_events,
        'n_front_shifts': n_front_shifts,
        'n_rear_shifts': n_rear_shifts,
        'has_gear_data': len(gear_events) > 0,
        'has_cadence': any('cadence' in r for r in records),
    }


def summarize(activity):
    """Resumen basico de una actividad ya parseada (sin depender de session/lap del FIT)."""
    records = activity['records']
    if not records:
        return {'n_points': 0}

    ts0 = records[0]['ts']
    ts1 = records[-1]['ts']
    distances = [r['distance'] for r in records if 'distance' in r]
    hrs = [r['heart_rate'] for r in records if 'heart_rate' in r]
    cads = [r['cadence'] for r in records if 'cadence' in r and r['cadence'] > 0]
    speeds = [r['speed'] for r in records if 'speed' in r]
    alts = [r['altitude'] for r in records if 'altitude' in r]
    powers = [r['power'] for r in records if 'power' in r]
    step_lengths = [r['step_length'] for r in records if 'step_length' in r]

    gain = 0.0
    loss = 0.0
    for a, b in zip(alts, alts[1:]):
        d = b - a
        if d > 0:
            gain += d
        else:
            loss += -d

    sport = activity.get('sport', 'cycling')
    return {
        'sport': sport,
        # etiqueta legible del tipo de actividad para la columna "Tipo" de la
        # lista (ver DISENO_ANALISIS_TRAMOS) -- Strava da un sport_type mas
        # fino (carretera/montaña/gravel...) que se sobreescribe en
        # strava_sync.py; el Karoo/FIT no distingue eso de forma fiable, asi
        # que aqui se deja un generico por deporte.
        'sport_detail': activity.get('sport_detail') or ('Running' if sport == 'running' else 'Ciclismo'),
        'n_points': len(records),
        'start_ts': ts0,
        'duration_s': ts1 - ts0,
        'distance_km': (max(distances) - min(distances)) / 1000.0 if distances else None,
        'elevation_gain_m': gain,
        'elevation_loss_m': loss,
        'avg_hr': sum(hrs) / len(hrs) if hrs else None,
        'max_hr': max(hrs) if hrs else None,
        'avg_cadence': sum(cads) / len(cads) if cads else None,
        'avg_speed_kmh': (sum(speeds) / len(speeds)) * 3.6 if speeds else None,
        'max_speed_kmh': max(speeds) * 3.6 if speeds else None,
        'avg_power': sum(powers) / len(powers) if powers else None,
        'avg_step_length_m': sum(step_lengths) / len(step_lengths) if step_lengths else None,
        'n_front_shifts': activity['n_front_shifts'],
        'n_rear_shifts': activity['n_rear_shifts'],
        'has_gear_data': activity['has_gear_data'],
        'has_cadence': activity['has_cadence'],
    }
