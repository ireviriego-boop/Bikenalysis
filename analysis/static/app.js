'use strict';

// ---------------------------------------------------------------- utils ----

function fmtDuration(s) {
  if (s == null) return '-';
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function fmtDate(ts) {
  if (ts == null) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' }) +
         ' ' + d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
}

function num(v, digits = 1) {
  return v == null ? '-' : Number(v).toFixed(digits);
}

// running se mide en ritmo (min/km), no en km/h -- ciclismo al reves.
function cadenceUnit(sport) {
  return sport === 'running' ? 'zpm' : 'rpm'; // zancadas/min, no "spm" (steps) -- app en español
}

function fmtSpeed(kmh, sport) {
  if (kmh == null || kmh <= 0) return '-';
  if (sport !== 'running') return `${num(kmh, 1)} km/h`;
  const paceMin = 60 / kmh;
  let m = Math.floor(paceMin);
  let s = Math.round((paceMin - m) * 60);
  if (s === 60) { s = 0; m += 1; }
  return `${m}:${String(s).padStart(2, '0')} /km`;
}

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  return e;
}

// ------------------------------------------------------------- app state ---

let RIDES = [];
let sortKey = 'start_ts';
let sortDir = -1;
let typeFilter = 'all';
let rideMap = null, rideMapLayer = null, hoverMarker = null;
let epMarkers = new Map(); // ep._id -> marcador Leaflet, para enlazar la lista de abajo con el mapa
window.addEventListener('resize', () => { if (rideMap) rideMap.invalidateSize(); });
let compareMap = null, compareMapLayers = [];
let PROFILE = {};
let chartMouseUpHandler = null; // ver renderRideChart -- arrastrar para elegir un tramo a mano

// ------------------------------------------------------------ navigation ---

function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(`view-${name}`).classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
}

document.querySelectorAll('.tab-btn').forEach(b => {
  b.addEventListener('click', () => {
    showView(b.dataset.view);
    // al volver a Ajustes (p.ej. tras completar el login en el navegador)
    // se refresca el estado por si ha cambiado.
    if (b.dataset.view === 'settings' && typeof loadConnections === 'function') loadConnections();
    if (b.dataset.view === 'fitness' && typeof loadFitness === 'function') loadFitness();
  });
});
document.getElementById('back-to-rides').addEventListener('click', () => showView('rides'));

// ------------------------------------------------------------- rides list --

const TYPE_ICON = { cycling: '🚴', running: '🏃' };

async function loadRides() {
  const res = await fetch('/api/rides');
  const data = await res.json();
  RIDES = data.summaries || [];
  populateTypeFilter();
  renderRidesTable();
  populateCompareSelects();
}

function populateTypeFilter() {
  const sel = document.getElementById('type-filter');
  const prev = sel.value || 'all';
  const types = [...new Set(RIDES.map(r => r.sport_detail).filter(Boolean))].sort();
  sel.innerHTML = '';
  sel.appendChild(el('option', { value: 'all' }, ['Todas']));
  for (const t of types) {
    sel.appendChild(el('option', { value: t }, [t]));
  }
  if (types.includes(prev) || prev === 'all') sel.value = prev;
}

function matchesTypeFilter(r) {
  return typeFilter === 'all' || r.sport_detail === typeFilter;
}

function renderRidesTable() {
  const tbody = document.getElementById('rides-tbody');
  tbody.innerHTML = '';
  const rows = RIDES.filter(matchesTypeFilter).sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av == null) return 1;
    if (bv == null) return -1;
    return av < bv ? -1 * sortDir : av > bv ? 1 * sortDir : 0;
  });
  for (const r of rows) {
    const tr = el('tr', {}, [
      el('td', {}, [fmtDate(r.start_ts)]),
      el('td', {}, [num(r.distance_km, 1)]),
      el('td', {}, [fmtDuration(r.duration_s)]),
      el('td', {}, [fmtSpeed(r.avg_speed_kmh, r.sport)]),
      el('td', {}, [r.avg_hr ? Math.round(r.avg_hr) + ' bpm' : '-']),
      el('td', {}, [Math.round(r.elevation_gain_m || 0) + ' m']),
      el('td', { class: 'type-cell' }, [`${TYPE_ICON[r.sport] || ''} ${r.sport_detail || '-'}`]),
    ]);
    tr.addEventListener('click', () => openRideDetail(r.activity_id));
    tbody.appendChild(tr);
  }
}

document.querySelectorAll('#rides-table thead th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = -1; }
    renderRidesTable();
  });
});

document.getElementById('type-filter').addEventListener('change', (e) => {
  typeFilter = e.target.value;
  renderRidesTable();
});

// ------------------------------------------------------------ ride detail --

const VERDICT_LABEL = { atrancado: 'Atrancado', fatiga: 'Fatiga', bien_ejecutado: 'Bien ejecutado' };
const VERDICT_COLOR = { atrancado: '#ff5c5c', fatiga: '#ffa94d', bien_ejecutado: '#6fcf97' };
const DIRECTION_LABEL = { climb: 'subida', descent: 'bajada' };

function verdictLabel(verdict) {
  return VERDICT_LABEL[verdict] || verdict;
}

function flagIcon(color) {
  const svg = `<svg width="18" height="22" viewBox="0 0 18 22" xmlns="http://www.w3.org/2000/svg">
    <line x1="2" y1="1" x2="2" y2="21" stroke="#1a1a1a" stroke-width="2"/>
    <path d="M2 2 L16 6 L2 10 Z" fill="${color}" stroke="#1a1a1a" stroke-width="1" stroke-linejoin="round"/>
  </svg>`;
  return L.divIcon({
    className: 'tramo-flag',
    html: svg,
    iconSize: [18, 22],
    iconAnchor: [2, 21],
    popupAnchor: [6, -18],
  });
}

function fmtLength(m) {
  if (m == null) return '-';
  return m >= 1000 ? `${num(m / 1000, 2)} km` : `${Math.round(m)} m`;
}

async function openRideDetail(activityId) {
  showView('ride-detail');
  document.getElementById('ride-detail-title').textContent = 'Cargando...';
  const res = await fetch(`/api/ride/${activityId}`);
  const data = await res.json();
  renderRideDetail(data);
}

function renderRideDetail(data) {
  const summary = RIDES.find(r => r.activity_id === data.activity_id) || {};
  document.getElementById('ride-detail-title').textContent =
    `${fmtDate(summary.start_ts)} — ${num(summary.distance_km, 1)} km`;

  const stats = document.getElementById('ride-stats');
  stats.innerHTML = '';
  const sport = summary.sport || 'cycling';
  const cards = [
    ['Distancia', num(summary.distance_km, 1) + ' km'],
    ['Duración', fmtDuration(summary.duration_s)],
    [sport === 'running' ? 'Ritmo medio' : 'Vel. media', fmtSpeed(summary.avg_speed_kmh, sport)],
    [sport === 'running' ? 'Ritmo máx' : 'Vel. máx', fmtSpeed(summary.max_speed_kmh, sport)],
    ['FC media', summary.avg_hr ? Math.round(summary.avg_hr) + ' bpm' : '-'],
    ['FC máx', summary.max_hr ? Math.round(summary.max_hr) + ' bpm' : '-'],
    ['Cadencia media', summary.avg_cadence ? Math.round(summary.avg_cadence) + ' ' + cadenceUnit(sport) : '-'],
    ['Desnivel +', Math.round(summary.elevation_gain_m || 0) + ' m'],
  ];
  // "Cambios de piñón" no tiene sentido en running (no hay marchas) -- solo
  // se muestra en ciclismo. La potencia, al reves, hoy solo hay datos reales
  // en running (ver DISENO_ANALISIS_TRAMOS seccion 6) -- en ciclismo siempre
  // saldria vacio porque ningun dispositivo del usuario la graba todavia.
  if (sport === 'cycling') {
    cards.push(['Cambios de piñón', summary.has_gear_data ? summary.n_rear_shifts : '-']);
  } else if (summary.avg_power) {
    cards.push(['Potencia media', `${Math.round(summary.avg_power)} W`]);
  }
  if (sport === 'running' && summary.avg_step_length_m) {
    // la zancada absoluta no dice mucho por si sola -- 85 cm no es lo mismo
    // en 1,90m que en 1,60m, asi que se muestra tambien como % de la altura
    // si el usuario la tiene configurada (Ajustes -> Perfil físico).
    const pct = PROFILE.height_cm ? ` (${num(summary.avg_step_length_m * 100 / PROFILE.height_cm * 100, 0)}% de tu altura)` : '';
    cards.push(['Zancada media', `${num(summary.avg_step_length_m, 2)} m${pct}`]);
  }
  for (const [label, value] of cards) {
    stats.appendChild(el('div', { class: 'stat-card' }, [
      el('div', { class: 'label' }, [label]),
      el('div', { class: 'value' }, [String(value)]),
    ]));
  }

  const fatigaBox = document.getElementById('ride-fatiga-context');
  const fc = data.fatiga_context;
  if (fc) {
    fatigaBox.hidden = false;
    fatigaBox.innerHTML = '';
    fatigaBox.appendChild(el('span', { class: `fatiga-badge forma-${fc.forma_label.replace(/ /g, '-')}` }, [
      `Llegabas a esta actividad ${fc.forma_label}`,
    ]));
    fatigaBox.appendChild(el('span', { class: 'fatiga-note' }, [
      ' -- solo informativo, no cambia el diagnóstico de los tramos de abajo.',
    ]));
  } else {
    fatigaBox.hidden = true;
    fatigaBox.innerHTML = '';
  }

  // Identificador estable por tramo (dentro de esta carga de la actividad):
  // deja que la lista de tramos de abajo y los marcadores del mapa se
  // refieran al mismo tramo sin ambiguedad al pinchar uno desde el otro.
  (data.interesting_points || []).forEach((ep, i) => { ep._id = i; });

  for (const fn of [() => renderRideMap(data), () => renderRideChart(data), () => renderInterestingList(data)]) {
    try { fn(); } catch (e) { console.error('fallo renderizando seccion de detalle:', e); }
  }
}

function focusTramo(data, ep) {
  clearCustomSelectionLayer();
  renderRideChart(data, ep);
  const marker = epMarkers.get(ep._id);
  if (marker && rideMap) {
    rideMap.panTo(marker.getLatLng());
    marker.openPopup();
  }
  document.getElementById('ride-map').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ------------------------------------------- tramo elegido a mano (drag) ---
// El motor de tramos automaticos (interesting_points.py) detecta tramos
// solo; esto deja al usuario arrastrar sobre la grafica para elegir CUALQUIER
// rango y analizarlo con el mismo motor (misma agregacion, mismo baseline,
// mismo "otras veces por aqui") -- ver DISENO_ANALISIS_TRAMOS.

let customSelectionLayer = null;

function clearCustomSelectionLayer() {
  if (customSelectionLayer && rideMap) rideMap.removeLayer(customSelectionLayer);
  customSelectionLayer = null;
}

async function showCustomTramo(data, startDistM, endDistM) {
  const focusLabel = document.getElementById('chart-focus');
  focusLabel.innerHTML = '';
  focusLabel.appendChild(el('span', { class: 'focus-badge' }, ['Analizando tramo seleccionado...']));

  const params = new URLSearchParams({
    activity_id: data.activity_id,
    start_distance_m: startDistM,
    end_distance_m: endDistM,
  });
  let ep;
  try {
    const res = await fetch(`/api/tramo-custom?${params}`);
    ep = await res.json();
  } catch (e) {
    ep = { error: 'Error de conexión con el servidor local.' };
  }
  if (ep.error) {
    focusLabel.innerHTML = '';
    focusLabel.appendChild(el('span', { class: 'focus-badge' }, [ep.error]));
    const resetBtn = el('button', { class: 'link-btn focus-reset' }, ['ver ruta completa']);
    resetBtn.addEventListener('click', () => { clearCustomSelectionLayer(); renderRideChart(data); });
    focusLabel.appendChild(resetBtn);
    return;
  }

  renderRideChart(data, ep);

  // resalta la seleccion sobre la propia ruta en el mapa, igual que un
  // tramo automatico, aunque este no tenga marcador propio (no viene del
  // motor de deteccion, no hay 'otras veces por aqui' que lo vincule a un
  // marcador ya existente).
  clearCustomSelectionLayer();
  const track = (data.track || []).filter(p => p.lat != null && p.lon != null && p.distance != null);
  const segPts = track.filter(p => p.distance >= startDistM && p.distance <= endDistM);
  if (segPts.length >= 2 && rideMap) {
    customSelectionLayer = L.polyline(segPts.map(p => [p.lat, p.lon]), {
      color: '#ffd166', weight: 7, opacity: 0.95,
    }).addTo(rideMap);
    rideMap.fitBounds(customSelectionLayer.getBounds(), { padding: [40, 40], maxZoom: 16 });
  }
}

function renderRideMap(data) {
  const track = (data.track || []).filter(p => p.lat != null && p.lon != null);
  if (!rideMap) {
    rideMap = L.map('ride-map');
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap',
      maxZoom: 19,
    }).addTo(rideMap);
  }
  if (rideMapLayer) rideMapLayer.remove();
  const group = L.layerGroup().addTo(rideMap);
  rideMapLayer = group;
  epMarkers = new Map();

  if (track.length) {
    const latlngs = track.map(p => [p.lat, p.lon]);
    L.polyline(latlngs, { color: '#4fb3ff', weight: 3 }).addTo(group);
    rideMap.fitBounds(latlngs, { padding: [16, 16] });
  }

  for (const ep of data.interesting_points || []) {
    const color = VERDICT_COLOR[ep.verdict] || '#fff';
    const popupHtml = `<b>${verdictLabel(ep.verdict)}</b> (${DIRECTION_LABEL[ep.direction] || ep.direction}, tier ${ep.reliability_tier})<br>` +
      `${ep.reason}<br>` +
      `longitud ${fmtLength((ep.end_distance_m ?? 0) - (ep.start_distance_m ?? 0))} · ${Math.round(ep.duration_s)}s · ` +
      `a ${fmtDuration(ep.elapsed_since_start_s)} de ruta<br>` +
      `pendiente ${num(ep.avg_grade_pct, 1)}% · cadencia ${ep.avg_cadence ? Math.round(ep.avg_cadence) + ' ' + cadenceUnit(data.sport) : '-'}` +
      (ep.avg_power ? ` · ${Math.round(ep.avg_power)} W` : '') +
      (ep.avg_rear_teeth ? ` · piñón ~${Math.round(ep.avg_rear_teeth)}T` : '');

    // Tramo coloreado sobre la propia ruta (no solo un punto): se recorta el
    // track por distancia acumulada entre el inicio y el fin del tramo.
    const tramoPts = track.filter(p =>
      p.distance != null && ep.start_distance_m != null && ep.end_distance_m != null &&
      p.distance >= ep.start_distance_m && p.distance <= ep.end_distance_m);
    if (tramoPts.length >= 2) {
      L.polyline(tramoPts.map(p => [p.lat, p.lon]), { color, weight: 6, opacity: 0.85 })
        .bindPopup(popupHtml)
        .on('click', () => focusTramo(data, ep))
        .addTo(group);
    }

    if (ep.lat == null || ep.lon == null) continue;
    const marker = L.marker([ep.lat, ep.lon], { icon: flagIcon(color) })
      .bindPopup(popupHtml)
      .on('click', () => focusTramo(data, ep))
      .addTo(group);
    epMarkers.set(ep._id, marker);
  }
}

function renderRideChart(data, focusEpisode) {
  const fullTrack = (data.track || []).filter(p => p.distance != null);
  const track = focusEpisode
    ? fullTrack.filter(p =>
        focusEpisode.start_distance_m != null && focusEpisode.end_distance_m != null &&
        p.distance >= focusEpisode.start_distance_m && p.distance <= focusEpisode.end_distance_m)
    : fullTrack;

  const focusLabel = document.getElementById('chart-focus');
  if (focusEpisode && track.length) {
    focusLabel.innerHTML = '';
    const badgeText = focusEpisode.verdict
      ? `Tramo: ${verdictLabel(focusEpisode.verdict)} (${DIRECTION_LABEL[focusEpisode.direction] || focusEpisode.direction})`
      : (focusEpisode.custom ? 'Tramo seleccionado (sin veredicto)' : 'Tramo');
    const badgeRow = el('div', { class: 'focus-badge-row' });
    badgeRow.appendChild(el('span', { class: `focus-badge verdict-${focusEpisode.verdict}` }, [badgeText]));
    const resetBtn = el('button', { class: 'link-btn focus-reset' }, ['ver ruta completa']);
    resetBtn.addEventListener('click', () => { clearCustomSelectionLayer(); renderRideChart(data); });
    badgeRow.appendChild(resetBtn);
    focusLabel.appendChild(badgeRow);
    if (focusEpisode.reason) {
      focusLabel.appendChild(el('div', { class: 'focus-reason' }, [focusEpisode.reason]));
    }
    // Resumen numerico del propio tramo enfocado -- un tramo automatico ya
    // lo lleva en el popup del mapa y en la lista de abajo, pero uno elegido
    // a mano (arrastrando) no tiene ninguno de los dos, asi que sin esto se
    // veian las cifras de "otras veces por aqui" pero nunca las de la propia
    // seleccion (lo que se estaba comparando).
    const statParts = [];
    if (focusEpisode.avg_grade_pct != null) statParts.push(`pendiente ${num(focusEpisode.avg_grade_pct, 1)}%`);
    statParts.push(`FC ${focusEpisode.avg_hr ? Math.round(focusEpisode.avg_hr) : '-'} bpm`);
    if (focusEpisode.avg_cadence) statParts.push(`cadencia ${Math.round(focusEpisode.avg_cadence)} ${cadenceUnit(data.sport)}`);
    if (focusEpisode.avg_power) statParts.push(`${Math.round(focusEpisode.avg_power)} W`);
    if (focusEpisode.avg_rear_teeth) statParts.push(`piñón ~${Math.round(focusEpisode.avg_rear_teeth)}T`);
    focusLabel.appendChild(el('div', { class: 'focus-stats' }, [statParts.join(' · ')]));
    document.getElementById('tramo-history').hidden = false;
    loadTramoHistory(data.activity_id, focusEpisode, data.sport);
  } else {
    focusLabel.innerHTML = '';
    // Oculto (no solo vacio): un panel vacio pero presente en el flex sigue
    // reservando su ancho, y con la ruta completa (sin tramo elegido) la
    // grafica debe poder usar todo el ancho disponible -- el panel de
    // historial solo tiene sentido junto a un tramo concreto.
    const historyPanel = document.getElementById('tramo-history');
    historyPanel.hidden = true;
    historyPanel.innerHTML = '';
  }

  // El punto que se ve al pasar el raton por la grafica queda de la vez
  // anterior si no se limpia aqui -- al cambiar de ruta o de tramo enfocado
  // se veria un punto suelto en un sitio que ya no tiene relacion con lo que
  // se esta mirando, hasta el siguiente hover.
  if (hoverMarker && rideMap) rideMap.removeLayer(hoverMarker);
  document.getElementById('chart-tooltip').hidden = true;

  const canvas = document.getElementById('ride-chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth || 600;
  const H = canvas.height = canvas.clientHeight || 220;
  ctx.clearRect(0, 0, W, H);
  if (!track.length) {
    canvas.onmousemove = null;
    canvas.onmouseleave = null;
    canvas.onmousedown = null;
    if (chartMouseUpHandler) { window.removeEventListener('mouseup', chartMouseUpHandler); chartMouseUpHandler = null; }
    return;
  }

  const dists = track.map(p => p.distance);
  const d0 = Math.min(...dists), d1 = Math.max(...dists);
  const xOf = d => 40 + (W - 50) * (d - d0) / Math.max(1, d1 - d0);

  const tooltip = document.getElementById('chart-tooltip');
  const canvasWrap = canvas.parentElement; // .chart-canvas-wrap, position:relative
  const selectionBox = document.getElementById('chart-selection');

  const pxToDist = (px) => d0 + (px - 40) / (W - 50) * (d1 - d0);

  // Arrastrar sobre la grafica elige un tramo a mano (cualquier rango, no
  // solo los que detecta el motor automatico) -- ver showCustomTramo. Un
  // arrastre minimo (< MIN_DRAG_PX) se trata como un simple click, no como
  // seleccion, para no disparar un analisis por un gesto accidental.
  const MIN_DRAG_PX = 6;
  let dragStartPx = null;

  function updateSelectionBox(x0, x1) {
    const left = Math.min(x0, x1), width = Math.max(2, Math.abs(x1 - x0));
    selectionBox.style.left = `${left}px`;
    selectionBox.style.width = `${width}px`;
    selectionBox.hidden = false;
  }

  // Mientras se arrastra, el tramo que va quedando seleccionado se resalta
  // en el mapa en vivo (y la bola de "por donde vas" se mueve al extremo
  // actual del arrastre) -- sin esto no hay forma de saber, mirando el mapa,
  // que tramo de la ruta se esta eligiendo realmente mientras se arrastra.
  function updateLiveSelectionOnMap(fromDist, toDist) {
    clearCustomSelectionLayer();
    const segPts = track.filter(p => p.lat != null && p.lon != null &&
      p.distance >= fromDist && p.distance <= toDist);
    if (segPts.length >= 2 && rideMap) {
      customSelectionLayer = L.polyline(segPts.map(p => [p.lat, p.lon]), {
        color: '#ffd166', weight: 7, opacity: 0.7, dashArray: '2 6',
      }).addTo(rideMap);
    }
    const end = segPts[segPts.length - 1];
    if (end && rideMap) {
      if (!hoverMarker) {
        hoverMarker = L.circleMarker([end.lat, end.lon], {
          radius: 8, color: '#fff', weight: 2, fillColor: '#ffd166', fillOpacity: 1,
        });
      } else {
        hoverMarker.setLatLng([end.lat, end.lon]);
      }
      if (!rideMap.hasLayer(hoverMarker)) hoverMarker.addTo(rideMap);
    }
  }

  canvas.onmousedown = (evt) => {
    const rect = canvas.getBoundingClientRect();
    dragStartPx = Math.max(40, Math.min(W - 10, evt.clientX - rect.left));
    updateSelectionBox(dragStartPx, dragStartPx);
    tooltip.hidden = true;
  };

  // Al pasar el raton por la grafica: resalta en el mapa el punto de la ruta
  // que corresponde a esa posicion (misma distancia acumulada) y muestra un
  // tooltip con los valores exactos ahi (pulso/velocidad/cadencia/altitud) --
  // los colores de las lineas por si solos no dejan leer el numero.
  canvas.onmousemove = (evt) => {
    const rect = canvas.getBoundingClientRect();
    const mouseX = evt.clientX - rect.left;

    if (dragStartPx != null) {
      const curPx = Math.max(40, Math.min(W - 10, mouseX));
      updateSelectionBox(dragStartPx, curPx);
      const fromDist = Math.min(pxToDist(dragStartPx), pxToDist(curPx));
      const toDist = Math.max(pxToDist(dragStartPx), pxToDist(curPx));
      updateLiveSelectionOnMap(fromDist, toDist);
      return;
    }

    const targetDist = pxToDist(mouseX);
    let nearest = null, bestDiff = Infinity;
    for (const p of track) {
      const diff = Math.abs(p.distance - targetDist);
      if (diff < bestDiff) { bestDiff = diff; nearest = p; }
    }
    if (!nearest) return;

    const rows = [];
    if (nearest.altitude != null) rows.push(`🟢 ${Math.round(nearest.altitude)} m`);
    if (nearest.heart_rate != null) rows.push(`🔴 ${Math.round(nearest.heart_rate)} bpm`);
    if (nearest.speed != null) rows.push(`🔵 ${fmtSpeed(nearest.speed * 3.6, data.sport)}`);
    if (nearest.cadence) rows.push(`🟣 ${Math.round(nearest.cadence)} ${cadenceUnit(data.sport)}`);
    if (nearest.power != null) rows.push(`🟡 ${Math.round(nearest.power)} W`);
    tooltip.innerHTML = `<div class="tt-dist">a ${num((nearest.distance || 0) / 1000, 2)} km</div>` +
      rows.map(r => `<div>${r}</div>`).join('');
    tooltip.hidden = false;
    const wrapRect = canvasWrap.getBoundingClientRect();
    let left = evt.clientX - wrapRect.left + 14;
    if (left + 110 > wrapRect.width) left = evt.clientX - wrapRect.left - 124;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${evt.clientY - wrapRect.top - 10}px`;

    if (!rideMap || nearest.lat == null || nearest.lon == null) return;
    if (!hoverMarker) {
      hoverMarker = L.circleMarker([nearest.lat, nearest.lon], {
        radius: 8, color: '#fff', weight: 2, fillColor: '#ffd166', fillOpacity: 1,
      });
    } else {
      hoverMarker.setLatLng([nearest.lat, nearest.lon]);
    }
    if (!rideMap.hasLayer(hoverMarker)) hoverMarker.addTo(rideMap);
  };
  canvas.onmouseleave = () => {
    if (dragStartPx == null) tooltip.hidden = true;
    if (hoverMarker && rideMap) rideMap.removeLayer(hoverMarker);
  };

  // El soltar el boton puede pasar fuera del canvas si se arrastra rapido --
  // se escucha en window, no en el canvas, para no perder ese caso. Se quita
  // el listener anterior antes de poner uno nuevo (cada renderRideChart crea
  // uno) para no ir acumulando manejadores duplicados entre renders.
  if (chartMouseUpHandler) window.removeEventListener('mouseup', chartMouseUpHandler);
  chartMouseUpHandler = (evt) => {
    if (dragStartPx == null) return;
    const rect = canvas.getBoundingClientRect();
    const endPx = Math.max(40, Math.min(W - 10, evt.clientX - rect.left));
    const startPx = dragStartPx;
    dragStartPx = null;
    selectionBox.hidden = true;
    if (Math.abs(endPx - startPx) < MIN_DRAG_PX) return; // gesto de click, no de arrastre
    const startDist = Math.min(pxToDist(startPx), pxToDist(endPx));
    const endDist = Math.max(pxToDist(startPx), pxToDist(endPx));
    showCustomTramo(data, startDist, endDist);
  };
  window.addEventListener('mouseup', chartMouseUpHandler);

  function drawSeries(field, color, yMin, yMax, padTop, padBottom) {
    const vals = track.map(p => p[field]);
    ctx.beginPath();
    let started = false;
    track.forEach((p, i) => {
      const v = p[field];
      if (v == null) { started = false; return; }
      const x = xOf(p.distance);
      const y = padTop + (H - padTop - padBottom) * (1 - (v - yMin) / Math.max(1e-6, (yMax - yMin)));
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.stroke();
  }

  const alts = track.map(p => p.altitude).filter(v => v != null);
  const hrs = track.map(p => p.heart_rate).filter(v => v != null);
  const speeds = track.map(p => p.speed).filter(v => v != null);
  const cads = track.map(p => p.cadence).filter(v => v != null && v > 0);
  const powers = track.map(p => p.power).filter(v => v != null);

  if (alts.length) drawSeries('altitude', '#6fcf97', Math.min(...alts) - 10, Math.max(...alts) + 10, 10, 110);
  if (hrs.length) drawSeries('heart_rate', '#ff5c5c', Math.min(...hrs) - 5, Math.max(...hrs) + 5, 90, 30);
  if (speeds.length) drawSeries('speed', '#4fb3ff', 0, Math.max(...speeds) * 1.1, 150, 5);
  if (cads.length) drawSeries('cadence', '#b389f9', 0, Math.max(...cads) * 1.15, 150, 5);
  if (powers.length) drawSeries('power', '#ffd166', 0, Math.max(...powers) * 1.15, 150, 5);

  ctx.strokeStyle = '#263646';
  ctx.beginPath(); ctx.moveTo(40, 0); ctx.lineTo(40, H); ctx.stroke();

  // La leyenda solo lista las lineas que de verdad hay en esta grafica --
  // mismo criterio que arriba (campos de la actividad): si no hay dato,
  // mejor no mostrar el campo/linea en absoluto, no un "-" hueco.
  const legend = document.querySelector('.chart-legend');
  if (legend) {
    const items = [['altitude', 'altitud', alts], ['hr', 'pulso', hrs], ['speed', 'velocidad', speeds],
                    ['cadence', 'cadencia', cads], ['power', 'potencia', powers]];
    legend.innerHTML = items.filter(([, , arr]) => arr.length)
      .map(([cls, label]) => `<span class="legend ${cls}">${label}</span>`).join('');
  }
}

let tramoHistoryRequestId = 0;

async function loadTramoHistory(activityId, ep, sport) {
  const panel = document.getElementById('tramo-history');
  const reqId = ++tramoHistoryRequestId;
  panel.innerHTML = '';
  panel.appendChild(el('div', { class: 'tramo-history-title' }, ['Otras veces por aquí']));
  panel.appendChild(el('div', { class: 'muted' }, ['Buscando...']));

  const params = new URLSearchParams({
    activity_id: activityId,
    start_distance_m: ep.start_distance_m,
    end_distance_m: ep.end_distance_m,
  });
  let data;
  try {
    const res = await fetch(`/api/tramo-history?${params}`);
    data = await res.json();
  } catch (e) {
    data = null;
  }
  if (reqId !== tramoHistoryRequestId) return; // se pidio otro tramo mientras tanto

  panel.innerHTML = '';
  panel.appendChild(el('div', { class: 'tramo-history-title' }, ['Otras veces por aquí']));
  panel.appendChild(el('div', { class: 'tramo-history-note' }, [
    'Solo informativo sobre este tramo exacto -- el veredicto de arriba compara con tu historial completo de subidas/bajadas, no con estas pasadas.',
  ]));
  const matches = data && data.matches || [];
  if (!data) {
    panel.appendChild(el('div', { class: 'muted' }, ['Error buscando el historial.']));
    return;
  }
  if (!matches.length) {
    panel.appendChild(el('div', { class: 'muted' }, ['Primera vez detectada por este tramo.']));
    return;
  }
  const curSpeed = ep.avg_speed_kmh;
  const ul = el('ul', { class: 'tramo-history-list' }, matches.map(m => {
    let dCls = '', dTxt = '';
    if (curSpeed != null && m.avg_speed_kmh != null) {
      if (sport === 'running') {
        // en ritmo, "mejor" es MENOS segundos/km (mas rapido), signo invertido
        // frente a la velocidad en km/h que usa ciclismo.
        const dSec = Math.round((60 / curSpeed - 60 / m.avg_speed_kmh) * 60);
        if (Math.abs(dSec) >= 2) { dCls = dSec < 0 ? 'better' : 'worse'; dTxt = ` (${dSec >= 0 ? '+' : ''}${dSec}s/km)`; }
      } else {
        const d = curSpeed - m.avg_speed_kmh;
        if (Math.abs(d) >= 0.3) { dCls = d > 0 ? 'better' : 'worse'; dTxt = ` (${d >= 0 ? '+' : ''}${num(d, 1)})`; }
      }
    }
    return el('li', {}, [
      el('div', { class: 'th-date' }, [fmtDate(m.start_ts)]),
      el('div', { class: 'th-stats' }, [
        fmtSpeed(m.avg_speed_kmh, sport),
        el('span', { class: dCls }, [dTxt]),
      ]),
      el('div', { class: 'th-meta' }, [
        `FC ${m.avg_hr ? Math.round(m.avg_hr) : '-'} bpm` +
        (m.avg_cadence ? ` · cad ${Math.round(m.avg_cadence)} ${cadenceUnit(sport)}` : '') +
        (m.avg_power ? ` · ${Math.round(m.avg_power)} W` : '') +
        (m.avg_rear_teeth ? ` · piñón ~${Math.round(m.avg_rear_teeth)}T` : ''),
      ]),
    ]);
  }));
  panel.appendChild(ul);
}

const TRAMO_GROUPS = [
  { key: 'problemas', label: '⚠ Problemas', match: e => e.verdict === 'atrancado' || e.verdict === 'fatiga' },
  { key: 'bien', label: '✓ Bien ejecutado', match: e => e.verdict === 'bien_ejecutado' },
];

function tramoListItem(data, ep) {
  const li = el('li', { class: `verdict-${ep.verdict}` }, [
    el('div', {}, [
      el('span', { class: 'badge' }, [verdictLabel(ep.verdict)]),
      ep.reason,
    ]),
    el('div', { class: 'meta' }, [
      `${DIRECTION_LABEL[ep.direction] || ep.direction} · tier ${ep.reliability_tier} · ` +
      `${Math.round(ep.duration_s)}s · a ${fmtDuration(ep.elapsed_since_start_s)} de ruta · ` +
      `pendiente media ${num(ep.avg_grade_pct, 1)}% · ` +
      `FC ${ep.avg_hr ? Math.round(ep.avg_hr) : '-'} bpm` +
      (ep.avg_cadence ? ` · cadencia ${Math.round(ep.avg_cadence)} ${cadenceUnit(data.sport)}` : '') +
      (ep.avg_power ? ` · ${Math.round(ep.avg_power)} W` : '') +
      (ep.avg_rear_teeth ? ` · piñón ~${Math.round(ep.avg_rear_teeth)}T` : '') +
      ` · a ${num((ep.distance_m || 0) / 1000, 1)} km`,
    ]),
  ]);
  li.addEventListener('click', () => focusTramo(data, ep));
  return li;
}

function renderInterestingList(data) {
  const episodes = data.interesting_points || [];
  document.getElementById('n-interesting').textContent = episodes.length;
  const wrap = document.getElementById('interesting-list');
  wrap.innerHTML = '';

  if (!episodes.length) {
    wrap.appendChild(el('p', { class: 'muted' }, ['No se han detectado tramos interesantes en esta actividad.']));
    return;
  }

  // Grupos plegables (cerrados por defecto) en vez de una lista fija: el
  // mapa ya deja ver el tramo que se pincha con su propio color/popup, asi
  // que mostrar aqui abajo, sin pedirlo, una lista de "problemas" sueltos
  // desconectada de lo que se esta mirando en el mapa solo confunde.
  for (const g of TRAMO_GROUPS) {
    const items = episodes.filter(g.match).sort((a, b) => a.reliability_tier - b.reliability_tier || b.duration_s - a.duration_s);
    if (!items.length) continue;
    const ul = el('ul', {}, items.map(ep => tramoListItem(data, ep)));
    const details = el('details', { class: `tramo-group group-${g.key}` }, [
      el('summary', {}, [`${g.label} (${items.length})`]),
      ul,
    ]);
    wrap.appendChild(details);
  }
}

// ---------------------------------------------------------------- compare --

function populateCompareSelects() {
  const opts = [...RIDES].sort((a, b) => (b.start_ts || 0) - (a.start_ts || 0)).map(r =>
    el('option', { value: r.activity_id }, [`${fmtDate(r.start_ts)} — ${num(r.distance_km, 1)} km`])
  );
  for (const id of ['compare-a', 'compare-b']) {
    const sel = document.getElementById(id);
    sel.innerHTML = '';
    opts.forEach((o, i) => sel.appendChild(o.cloneNode(true)));
  }
  if (RIDES.length > 1) document.getElementById('compare-b').selectedIndex = 1;
}

document.getElementById('compare-btn').addEventListener('click', runCompare);

async function runCompare() {
  const a = document.getElementById('compare-a').value;
  const b = document.getElementById('compare-b').value;
  if (!a || !b || a === b) {
    document.getElementById('compare-summary').textContent = 'Elige dos actividades distintas.';
    return;
  }
  document.getElementById('compare-summary').textContent = 'Comparando...';
  const [cmpRes, trackA, trackB] = await Promise.all([
    fetch(`/api/compare?a=${a}&b=${b}`).then(r => r.json()),
    fetch(`/api/ride/${a}`).then(r => r.json()),
    fetch(`/api/ride/${b}`).then(r => r.json()),
  ]);
  renderCompare(cmpRes, trackA, trackB);
}

function renderCompare(cmp, trackA, trackB) {
  const summaryDiv = document.getElementById('compare-summary');
  if (cmp.error) { summaryDiv.textContent = 'Error: ' + cmp.error; return; }
  summaryDiv.textContent =
    `${cmp.n_shared_segments} tramo(s) compartido(s), ${num(cmp.total_shared_distance_m / 1000, 1)} km en común.`;

  try {
    if (!compareMap) {
      compareMap = L.map('compare-map');
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap', maxZoom: 19,
      }).addTo(compareMap);
    }
    compareMapLayers.forEach(l => l.remove());
    compareMapLayers = [];

    const bounds = [];
    function addTrack(track, color) {
      const pts = (track.track || []).filter(p => p.lat != null && p.lon != null).map(p => [p.lat, p.lon]);
      if (!pts.length) return;
      const layer = L.polyline(pts, { color, weight: 2, opacity: 0.55 }).addTo(compareMap);
      compareMapLayers.push(layer);
      bounds.push(...pts);
    }
    addTrack(trackA, '#4fb3ff');
    addTrack(trackB, '#6fcf97');

    for (const seg of cmp.segments || []) {
      const path = (seg.b.path && seg.b.path.length ? seg.b.path : seg.a.path) || [];
      if (!path.length) continue;
      const layer = L.polyline(path, { color: '#ffd84d', weight: 5, opacity: 0.9 }).addTo(compareMap);
      compareMapLayers.push(layer);
    }
    if (bounds.length) compareMap.fitBounds(bounds, { padding: [16, 16] });
  } catch (e) {
    console.error('fallo renderizando el mapa de comparacion:', e);
  }

  const container = document.getElementById('compare-segments');
  container.innerHTML = '';
  if (!(cmp.segments || []).length) {
    container.appendChild(el('div', {}, ['No se han encontrado tramos compartidos entre estas dos actividades.']));
    return;
  }
  cmp.segments.forEach((seg, i) => {
    const a = seg.a, b = seg.b;
    function row(label, va, vb, fmt = v => v, lowerIsBetter = null) {
      const fa = fmt(va), fb = fmt(vb);
      let clsA = '', clsB = '';
      if (lowerIsBetter !== null && va != null && vb != null && va !== vb) {
        const aBetter = lowerIsBetter ? va < vb : va > vb;
        clsA = aBetter ? 'better' : 'worse';
        clsB = aBetter ? 'worse' : 'better';
      }
      return el('tr', {}, [
        el('td', {}, [label]),
        el('td', { class: clsA }, [String(fa)]),
        el('td', { class: clsB }, [String(fb)]),
      ]);
    }
    const table = el('table', { class: 'segment-compare-table' }, [
      el('tr', {}, [el('th', {}, ['']), el('th', {}, ['A']), el('th', {}, ['B'])]),
      row('Distancia', a.distance_m, b.distance_m, v => num(v / 1000, 2) + ' km'),
      row('Duración', a.duration_s, b.duration_s, fmtDuration, true),
      row('Vel. media', a.avg_speed_kmh, b.avg_speed_kmh, v => num(v, 1) + ' km/h', false),
      row('FC media', a.avg_hr, b.avg_hr, v => v ? Math.round(v) + ' bpm' : '-', true),
      row('Cadencia media', a.avg_cadence, b.avg_cadence, v => v ? Math.round(v) + ' rpm' : '-'),
      row('Desnivel +', a.elevation_gain_m, b.elevation_gain_m, v => Math.round(v) + ' m'),
    ]);
    container.appendChild(el('div', { class: 'segment-card' }, [
      el('h4', {}, [`Tramo ${i + 1} · ${num((b.distance_m || 0) / 1000, 2)} km · ${seg.direction_b_vs_a === 'same' ? 'mismo sentido' : 'sentido contrario'}`]),
      table,
    ]));
  });
}

// -------------------------------------------------------------------- sync -

const syncBtn = document.getElementById('sync-btn');
const syncStatus = document.getElementById('sync-status');

syncBtn.addEventListener('click', async () => {
  syncBtn.disabled = true;
  syncStatus.className = '';
  syncStatus.textContent = 'Sincronizando con Hammerhead...';
  try {
    const res = await fetch('/api/sync', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) {
      syncStatus.className = 'error';
      syncStatus.textContent = data.error || 'Fallo la sincronización.';
      return;
    }
    syncStatus.className = 'ok';
    syncStatus.textContent = data.new_count > 0
      ? `${data.new_count} actividad(es) nueva(s) descargada(s) y analizada(s).`
      : 'Ya estaba todo al día, sin actividades nuevas.';
    await loadRides();
  } catch (e) {
    syncStatus.className = 'error';
    syncStatus.textContent = 'Error de conexión con el servidor local.';
  } finally {
    syncBtn.disabled = false;
  }
});

// ----------------------------------------------------------------- ajustes -

const CONNECTION_LABELS = { hammerhead: 'Hammerhead', strava: 'Strava' };

async function loadConnections() {
  const wrap = document.getElementById('connections-list');
  let statuses;
  try {
    const res = await fetch('/api/connections');
    statuses = await res.json();
  } catch (e) {
    wrap.innerHTML = '';
    wrap.appendChild(el('div', { class: 'muted' }, ['Error consultando el estado de las conexiones.']));
    return;
  }
  wrap.innerHTML = '';
  for (const [name, label] of Object.entries(CONNECTION_LABELS)) {
    const st = statuses[name] || {};
    const row = el('div', { class: 'connection-row' });
    const dot = el('span', { class: `connection-dot ${st.connected ? 'ok' : ''}` });
    const text = el('span', { class: 'connection-text' }, [
      `${label}: ${st.connected ? 'conectado' : st.configured ? 'no conectado' : 'no configurado'}`,
    ]);
    row.appendChild(dot);
    row.appendChild(text);

    if (!st.configured) {
      row.appendChild(el('span', { class: 'connection-hint' }, [
        `Falta configurar las credenciales en .env (ver README para crear una app en ${label === 'Hammerhead' ? 'dashboard.hammerhead.io' : 'strava.com/settings/api'}).`,
      ]));
    } else {
      const btn = el('button', { class: 'sync-btn' }, [st.connected ? 'Reconectar' : 'Conectar']);
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        text.textContent = `${label}: abriendo navegador...`;
        try {
          const res = await fetch(`/api/connect/${name}`, { method: 'POST' });
          const data = await res.json();
          text.textContent = data.ok
            ? `${label}: completa el login en el navegador y vuelve aquí`
            : `${label}: ${data.error || 'fallo al conectar'}`;
        } catch (e) {
          text.textContent = `${label}: error de conexión con el servidor local`;
        } finally {
          btn.disabled = false;
        }
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
  }
}

const SETTINGS_FIELDS = {
  max_grade_divergence_pct: 'setting-grade-divergence',
  max_tramo_distance_m: 'setting-max-distance',
  baseline_window_days: 'setting-window-days',
};

async function loadSettings() {
  const res = await fetch('/api/settings');
  const settings = await res.json();
  for (const [key, id] of Object.entries(SETTINGS_FIELDS)) {
    document.getElementById(id).value = settings[key];
  }
}

const settingsSaveBtn = document.getElementById('settings-save-btn');
const settingsStatus = document.getElementById('settings-status');

settingsSaveBtn.addEventListener('click', async () => {
  const body = {};
  for (const [key, id] of Object.entries(SETTINGS_FIELDS)) {
    body[key] = Number(document.getElementById(id).value);
  }
  settingsSaveBtn.disabled = true;
  settingsStatus.className = '';
  settingsStatus.textContent = 'Guardando y recalculando (baselines + tramos de todas las actividades)...';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.ok) {
      settingsStatus.className = 'error';
      settingsStatus.textContent = data.error || 'Fallo al guardar los ajustes.';
      return;
    }
    settingsStatus.className = 'ok';
    settingsStatus.textContent = 'Guardado y recalculado. Vuelve a abrir una actividad para ver el efecto.';
  } catch (e) {
    settingsStatus.className = 'error';
    settingsStatus.textContent = 'Error de conexión con el servidor local.';
  } finally {
    settingsSaveBtn.disabled = false;
  }
});

// --------------------------------------------------- perfil fisico (TRIMP) -

async function loadProfile() {
  const res = await fetch('/api/profile');
  const profile = await res.json();
  PROFILE = profile;
  document.getElementById('profile-resting-hr').value = profile.resting_hr ?? '';
  document.getElementById('profile-height').value = profile.height_cm ?? '';
  const weightDateEl = document.getElementById('weight-date');
  if (!weightDateEl.value) weightDateEl.valueAsDate = new Date();
  renderWeightHistory(profile.weight_history || []);
}

const profileSaveBtn = document.getElementById('profile-save-btn');
const profileStatus = document.getElementById('profile-status');

profileSaveBtn.addEventListener('click', async () => {
  const restingHr = document.getElementById('profile-resting-hr').value;
  const heightCm = document.getElementById('profile-height').value;
  profileSaveBtn.disabled = true;
  profileStatus.className = '';
  profileStatus.textContent = 'Guardando...';
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resting_hr: restingHr, height_cm: heightCm }),
    });
    const data = await res.json();
    if (!data.ok) {
      profileStatus.className = 'error';
      profileStatus.textContent = data.error || 'Fallo al guardar el perfil.';
      return;
    }
    PROFILE = data.profile;
    profileStatus.className = 'ok';
    profileStatus.textContent = 'Guardado (recalculando Forma en segundo plano).';
  } catch (e) {
    profileStatus.className = 'error';
    profileStatus.textContent = 'Error de conexión con el servidor local.';
  } finally {
    profileSaveBtn.disabled = false;
  }
});

// ------------------------------------------------------- peso (historial) -

function renderWeightHistory(history) {
  const wrap = document.getElementById('weight-history-list');
  wrap.innerHTML = '';
  for (const entry of [...history].reverse().slice(0, 10)) {
    const dateStr = new Date(entry.date + 'T12:00:00').toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' });
    const row = el('div', { class: 'weight-row' }, [
      el('span', { class: 'weight-date' }, [dateStr]),
      el('span', { class: 'weight-kg' }, [`${num(entry.weight_kg, 1)} kg`]),
    ]);
    const delBtn = el('button', { class: 'link-btn weight-del' }, ['borrar']);
    delBtn.addEventListener('click', async () => {
      delBtn.disabled = true;
      try {
        const res = await fetch('/api/profile/weight/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: entry.date }),
        });
        const data = await res.json();
        if (data.ok) { PROFILE = data.profile; renderWeightHistory(PROFILE.weight_history || []); }
      } catch (e) { /* deja el boton reactivarse abajo si falla */ }
      delBtn.disabled = false;
    });
    row.appendChild(delBtn);
    wrap.appendChild(row);
  }
  if (!history.length) {
    wrap.appendChild(el('div', { class: 'muted' }, ['Todavía no has registrado ningún peso.']));
  }
}

const weightSaveBtn = document.getElementById('weight-save-btn');
const weightStatus = document.getElementById('weight-status');

weightSaveBtn.addEventListener('click', async () => {
  const weightKg = document.getElementById('weight-input').value;
  const dateInput = document.getElementById('weight-date').value;
  if (!weightKg) return;
  weightSaveBtn.disabled = true;
  weightStatus.className = '';
  weightStatus.textContent = 'Guardando...';
  try {
    const res = await fetch('/api/profile/weight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateInput || undefined, weight_kg: weightKg }),
    });
    const data = await res.json();
    if (!data.ok) {
      weightStatus.className = 'error';
      weightStatus.textContent = data.error || 'Fallo al guardar el peso.';
      return;
    }
    PROFILE = data.profile;
    renderWeightHistory(PROFILE.weight_history || []);
    document.getElementById('weight-input').value = '';
    weightStatus.className = 'ok';
    weightStatus.textContent = 'Guardado (recalculando vatios/kg en segundo plano).';
  } catch (e) {
    weightStatus.className = 'error';
    weightStatus.textContent = 'Error de conexión con el servidor local.';
  } finally {
    weightSaveBtn.disabled = false;
  }
});

document.getElementById('fitness-goto-settings').addEventListener('click', () => {
  document.querySelector('.tab-btn[data-view="settings"]').click();
});

// ---------------------------------------------- Fitness / Fatiga / Forma ---

const FORMA_LABEL_TEXT = {
  'muy fresco': '💪 muy fresco',
  fresco: '🙂 fresco',
  normal: '😐 normal',
  cansado: '😓 cansado',
  'muy cansado': '🥵 muy cansado',
};

const RECOMMENDATION_CLASS = {
  'muy fresco': 'rec-good',
  fresco: 'rec-good',
  normal: 'rec-normal',
  cansado: 'rec-caution',
  'muy cansado': 'rec-rest',
};

async function loadFitness() {
  const res = await fetch('/api/fitness');
  const data = await res.json();

  const unconfigured = document.getElementById('fitness-unconfigured');
  const content = document.getElementById('fitness-content');
  if (!data.configured || !data.days || !data.days.length) {
    unconfigured.hidden = false;
    content.hidden = true;
    return;
  }
  unconfigured.hidden = true;
  content.hidden = false;

  const cur = data.current;
  document.getElementById('forma-value').textContent = (cur.forma > 0 ? '+' : '') + num(cur.forma, 0);
  document.getElementById('forma-label').textContent = FORMA_LABEL_TEXT[cur.forma_label] || cur.forma_label;
  document.getElementById('forma-value').className = `forma-value forma-${cur.forma_label.replace(/ /g, '-')}`;
  document.getElementById('detail-fitness').textContent = num(cur.fitness, 0);
  document.getElementById('detail-fatiga').textContent = num(cur.fatiga, 0);

  const recEl = document.getElementById('forma-recommendation');
  const rec = cur.recommendation;
  if (rec) {
    recEl.className = `forma-recommendation ${RECOMMENDATION_CLASS[rec.label] || 'rec-normal'}`;
    recEl.innerHTML = `${rec.message}<span class="rec-caveat">Basado solo en pulso -- no tiene en cuenta sueño, enfermedad u otros factores; es una guía, no una prescripción.</span>`;
  } else {
    recEl.innerHTML = '';
  }

  renderFitnessChart(data.days);
  renderFitnessRecentList(data.recent);
}

// Fecha (dia) actualmente resaltado en la grafica con una linea vertical --
// por click desde la lista de "ultimos dias" de abajo, o por hover sobre la
// propia grafica (ver renderFitnessChart). Vive fuera de la funcion para que
// el manejador de click de la lista pueda volver a dibujar sin recalcular
// todo desde el API.
let fitnessChartDays = [];
let fitnessChartDraw = null;
let fitnessSelectedDate = null;

function fmtDayLong(iso) {
  return new Date(iso + 'T12:00:00').toLocaleDateString('es-ES', { weekday: 'long', day: '2-digit', month: 'long' });
}

function renderFitnessChart(days) {
  const canvas = document.getElementById('fitness-chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.width = canvas.clientWidth || 800;
  const H = canvas.height = canvas.clientHeight || 220;

  // solo los ultimos ~6 meses -- todo el historico (aqui, años via Strava)
  // aplastaria la parte reciente, que es la que importa para "como estoy
  // ahora", en un puñado de pixels.
  const recent = days.slice(-183);
  fitnessChartDays = recent;
  if (!recent.length) { fitnessChartDraw = null; return; }

  const allVals = recent.flatMap(d => [d.fitness, d.fatiga, d.forma]);
  const yMin = Math.min(0, ...allVals), yMax = Math.max(...allVals, 1);
  const padL = 40, padR = 10, padT = 10, padB = 10;
  const xOf = i => padL + (W - padL - padR) * i / Math.max(1, recent.length - 1);
  const yOf = v => H - padB - (H - padT - padB) * (v - yMin) / Math.max(1, yMax - yMin);

  function draw(highlightIdx) {
    ctx.clearRect(0, 0, W, H);

    // linea del cero (Forma puede ser negativa)
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, yOf(0));
    ctx.lineTo(W - padR, yOf(0));
    ctx.stroke();

    function drawSeries(field, color, width) {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      recent.forEach((d, i) => {
        const x = xOf(i), y = yOf(d[field]);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    drawSeries('forma', '#ffd166', 1.5);
    drawSeries('fatiga', '#ff6b6b', 1.5);
    drawSeries('fitness', '#4fb3ff', 2.5);

    // eje Y: min/max/0 para poder leer la escala sin marcas intermedias
    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'right';
    [yMin, 0, yMax].forEach(v => ctx.fillText(Math.round(v), padL - 6, yOf(v) + 4));

    // dia resaltado (click desde la lista de abajo, o hover) -- una linea
    // vertical que marca exactamente que dia de la grafica corresponde a esa
    // actividad/fila, al estilo de Strava/Elevate.
    if (highlightIdx != null && recent[highlightIdx]) {
      const x = xOf(highlightIdx);
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, H - padB);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  fitnessChartDraw = draw;
  const initialIdx = fitnessSelectedDate ? recent.findIndex(d => d.date === fitnessSelectedDate) : -1;
  draw(initialIdx >= 0 ? initialIdx : null);

  const tooltip = document.getElementById('fitness-chart-tooltip');
  const wrap = canvas.parentElement;

  canvas.onmousemove = (evt) => {
    const rect = canvas.getBoundingClientRect();
    const mouseX = evt.clientX - rect.left;
    const idxFloat = (mouseX - padL) / (W - padL - padR) * (recent.length - 1);
    const idx = Math.max(0, Math.min(recent.length - 1, Math.round(idxFloat)));
    const d = recent[idx];
    if (!d) return;
    draw(idx);
    tooltip.innerHTML = `<div class="tt-dist">${fmtDayLong(d.date)}</div>` +
      `<div>🔵 fitness ${num(d.fitness, 0)}</div>` +
      `<div>🔴 fatiga ${num(d.fatiga, 0)}</div>` +
      `<div>🟡 forma ${d.forma > 0 ? '+' : ''}${num(d.forma, 0)}</div>` +
      (d.trimp > 0 ? `<div>TRIMP del día: ${num(d.trimp, 0)}</div>` : '<div>día de descanso</div>');
    tooltip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    let left = evt.clientX - wrapRect.left + 14;
    if (left + 150 > wrapRect.width) left = evt.clientX - wrapRect.left - 160;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${evt.clientY - wrapRect.top - 10}px`;
  };
  canvas.onmouseleave = () => {
    tooltip.hidden = true;
    const selIdx = fitnessSelectedDate ? recent.findIndex(d => d.date === fitnessSelectedDate) : -1;
    draw(selIdx >= 0 ? selIdx : null);
  };
}

function highlightFitnessDay(dateIso) {
  fitnessSelectedDate = dateIso;
  document.querySelectorAll('.fitness-recent-row').forEach(row => {
    row.classList.toggle('active', row.dataset.date === dateIso);
  });
  if (fitnessChartDraw) {
    const idx = fitnessChartDays.findIndex(d => d.date === dateIso);
    fitnessChartDraw(idx >= 0 ? idx : null);
  }
  document.querySelector('.fitness-chart-wrap')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderFitnessRecentList(recent) {
  const wrap = document.getElementById('fitness-recent-list');
  wrap.innerHTML = '';
  for (const d of [...recent].reverse()) {
    const dateStr = new Date(d.date + 'T12:00:00').toLocaleDateString('es-ES', { weekday: 'short', day: '2-digit', month: 'short' });
    const effect = d.trimp <= 0 ? 'descanso' : d.trimp < 40 ? 'suave' : d.trimp < 90 ? 'moderada' : 'fuerte';
    const row = el('div', { class: 'fitness-recent-row', 'data-date': d.date }, [
      el('span', { class: 'frr-date' }, [dateStr]),
      el('span', { class: `frr-effect frr-${effect}` }, [effect]),
      el('span', { class: 'frr-trimp' }, [d.trimp > 0 ? `TRIMP ${num(d.trimp, 0)}` : '-']),
      el('span', { class: 'frr-forma' }, [`forma ${d.forma > 0 ? '+' : ''}${num(d.forma, 0)}`]),
    ]);
    row.addEventListener('click', () => highlightFitnessDay(d.date));
    wrap.appendChild(row);
  }
}

// ------------------------------------------------------------------ init ---

loadRides();
loadSettings();
loadConnections();
loadProfile();
