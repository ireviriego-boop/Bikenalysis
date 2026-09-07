# Bikenalysis — diseño v2 del motor de análisis (por tramos)

Notas de la conversación de diseño del 7 de septiembre de 2026, antes de implementarlo
en código. Sustituye/amplía el enfoque anterior de `interesting_points.py`, que
clasificaba **instante a instante** y luego agrupaba puntos consecutivos en episodios.
El problema detectado: un cambio de marcha anticipado (p.ej. subir de piñón antes de
que empiece de verdad un repecho, o bajar de piñón antes de una bajada) genera unos
pocos metros de cadencia muy alta o muy baja que no reflejan un problema real, y el
análisis punto a punto los puede marcar como "atrancado" por error.

## 1. Segmentación en tramos (en vez de puntos sueltos)

La ruta se trocea en tramos, y el análisis se hace sobre la media/agregado de cada
tramo, no sobre instantes sueltos. Cómo se definen los cortes del tramo depende de
qué datos haya disponibles:

**Con datos de piñón (Di2/AXS/EPS u otro grupo electrónico registrado):**
- Un tramo empieza en un cambio de marcha.
- Se extiende mientras los cambios sucesivos vayan en la misma dirección (todos
  hacia piñón más grande, o todos hacia más pequeño). En cuanto un cambio invierte
  la dirección de la racha, se cierra el tramo ahí.
- Guarda adicional de pendiente: la pendiente de cada punto nuevo no debe alejarse
  más de **X puntos porcentuales** de la media del tramo acumulada hasta ese punto
  (no del primer punto — así un desvío lento y gradual sí puede cortar). Ejemplo: no
  debería fusionarse un tramo que pasa de 6% a 15% de pendiente, aunque la dirección
  de los cambios de piñón sea consistente.
- `X` (divergencia máxima de pendiente) es un **setting configurable**.

**Sin datos de piñón (la mayoría de ciclistas):**
- Mismo mecanismo, pero sin la condición de dirección de piñón: se segmenta
  directamente por fases de pendiente sostenida, cortando cuando la pendiente se
  aleja más de `X` puntos de la media del tramo. Pendiente siempre está disponible
  en cualquier FIT con GPS+altitud, así que este es el segmentador universal de
  respaldo.

**Tope de seguridad (aplica siempre):**
- Si no hay ningún corte (ni cambio de piñón ni cambio de fase de pendiente) durante
  mucho rato — p.ej. una marcha mantenida kilómetros — hace falta una **distancia
  máxima sin cambios** antes de cortar el tramo de todas formas, para no acabar con
  un tramo de 10 km tratado como una sola unidad. También configurable.

## 2. Señales y niveles de fiabilidad (tiers)

Jerarquía de más a menos preciso, según qué sensores tenga el ciclista. Cada nivel
usa los baselines personales (percentiles) correspondientes a esas señales:

1. **Potencia** (+ el resto si lo hay). Señal más fiable: mide el esfuerzo real al
   instante, sin el retraso fisiológico del pulso ni la dependencia de la marcha que
   tienen cadencia/piñón. Si además hay pulso, se abre una categoría nueva de aviso:
   "misma potencia de siempre pero el pulso se ha disparado" → señal de fatiga
   acumulada, distinta de "vas atrancado".
   - **No hay datos reales de potencia todavía** (el usuario no tiene medidor). Se
     diseña la rama igualmente, dejando el hueco listo en el formato de datos, pero
     la lógica de clasificación no se valida hasta que haya un FIT real con potencia.
2. **Piñón + cadencia + pulso + pendiente** (el caso ideal actual del usuario,
   Di2 + Ki2 + sensor de cadencia).
3. **Piñón O cadencia (uno de los dos) + pulso + pendiente**.
4. **Solo pulso + pendiente** (el suelo de hoy — la mayoría de las 116 salidas
   actuales del usuario están aquí, por no tener piñón/cadencia).
5. **Solo GPS + velocidad + pendiente** (sin pulso ni ningún sensor extra — el
   mínimo absoluto que graba cualquier ciclocomputador GPS). Señal: comparar la
   velocidad del tramo contra el baseline personal de velocidad para esa pendiente.
   Es el nivel que garantiza que la app sirva para cualquier ciclista, tenga los
   sensores que tenga.

### Cobertura de bajadas (laguna del motor actual)

El clasificador de hoy descarta de raíz cualquier punto que no esté en subida
(`grade < umbral` → fuera). Se añade el patrón espejo para bajadas: piñón grande
(marcha larga/rápida) que obliga a cadencia muy alta en descenso porque no se puede
pedalear más despacio con esa relación ("no puedes bajar el ritmo"). Usa baselines de
bajada (cadencia y piñón en descenso) igual que ya existen para subida.

### Tramos "bien ejecutados"

Además de marcar problemas, el motor debe poder marcar tramos donde todo fue "según
lo esperado" (piñón adecuado a la pendiente, cadencia en rango cómodo, pulso sin
dispararse) — mismas señales y mismos tiers, pero en positivo. Da feedback útil más
allá de solo avisos, y sirve como confirmación de que el sistema está leyendo bien
los datos cuando una salida sale limpia.

## 3. Baseline adaptativo en el tiempo

Problema: los baselines se calculan hoy sobre todo el histórico por igual. Si el
ciclista mejora (o empeora) su forma física con el tiempo, salidas de hace meses
siguen pesando igual que las recientes, y el "normal" que usa la app queda
desactualizado.

**Solución propuesta: ventana móvil.**
- Calcular los percentiles solo con las salidas de los **últimos X días** (punto de
  partida razonable: ~90 días) en vez de con todo el histórico.
- `X` es otro setting configurable.
- Colchón para usuarios nuevos o con pocas salidas recientes: si la ventana no tiene
  datos suficientes todavía, usar el histórico completo como respaldo hasta que se
  acumule masa suficiente en la ventana.

**Alternativa más fina (no es el punto de partida, pero queda anotada):**
- En vez de un corte duro de ventana (que puede producir un salto brusco el día que
  una salida "se cae" fuera de la ventana), usar un peso decreciente gradual por
  antigüedad (decaimiento) en vez de una ventana con corte seco. Más complejo de
  calcular (percentiles ponderados), se deja como posible mejora futura si el efecto
  salto de la ventana simple resulta molesto en la práctica.

**Efecto secundario aprovechable:** si se calcula un baseline que se mueve con el
tiempo, sale casi gratis una gráfica de evolución de forma física (p.ej. cómo ha
cambiado el p50 de pulso en subida mes a mes) — no es parte de este diseño, pero se
apunta porque sale del mismo cálculo.

## 4. Resumen de settings configurables que introduce este diseño

- Divergencia máxima de pendiente para seguir considerando el mismo tramo (puntos %).
- Distancia máxima sin cambios de piñón/fase de pendiente antes de cortar el tramo
  de todas formas.
- Ventana de días para el baseline adaptativo (con respaldo a histórico completo si
  no hay datos suficientes en la ventana).

## 5. Multideporte: ciclismo + running

Idea añadida tras enseñar la app a un compañero que solo corre (no monta en bici).
Encaja bien en el diseño ya planteado porque buena parte del motor es agnóstica al
deporte:

- **Lo que ya es reutilizable tal cual:** el motor de comparación de tramos
  (`segment_match.py`) trabaja solo con lat/lon/distancia, así que sirve igual para
  comparar dos carreras a pie que dos salidas en bici. El sistema de baselines
  personales por percentil, con ventana adaptativa (sección 3), también es el mismo
  mecanismo aplicado a otras métricas.
- **Lo que hay que adaptar por deporte:** las métricas relevantes cambian. Running no
  tiene piñón, así que el equivalente al "atrancado" sería comparar el ritmo
  (min/km) o el pulso contra el baseline propio **para esa pendiente concreta** —
  misma idea de "baseline condicionado por pendiente" que ya usamos en ciclismo
  (`climbing_hr`, etc.), solo que la señal de esfuerzo es ritmo/pulso en vez de
  piñón/cadencia. Si el reloj del compañero graba dinámica de carrera (tiempo de
  contacto con el suelo, oscilación vertical, longitud de zancada), esas señales
  añadirían tiers extra, igual que piñón/cadencia/potencia hacen en ciclismo.
- **Identificar el deporte de cada actividad:** el propio FIT ya trae un campo de
  deporte (running/ciclismo/etc.) en el mensaje de sesión, así que no hace falta
  inventar nada para etiquetar cada actividad — solo empezar a leerlo (hoy el
  parser no lo usa porque todo lo procesado hasta ahora es ciclismo).
- **En la interfaz:** dos secciones (ciclismo / running), cada una con sus propias
  unidades y paneles (el panel de piñón no tendría sentido en running, por ejemplo),
  pero compartiendo la misma pantalla de comparación de tramos y el mismo concepto
  de episodios "interesantes" por tramo.
- **Ojo con la "potencia estimada" que muestra Strava en las salidas de bici:**
  verificado (campo por campo en el FIT del Karoo, y también en el GPX exportado de
  Strava) que ese dato NO existe en ningún fichero — ni el Karoo lo graba (no hay
  medidor de potencia real en la bici) ni el GPX lo trae. Es un cálculo que hace el
  propio Strava en su web a partir de peso + velocidad + altimetría (aparece bajo
  "Suscripción" en la interfaz de Strava, con el aviso "estimada" en el propio
  gráfico) — no viene de Ki2/Karoo ni de ninguna extensión del navegador, y nunca se
  guarda en el fichero. No sirve como fuente real para la rama de potencia de
  ciclismo (sección de más abajo): al salir de la misma velocidad+pendiente que ya
  tenemos, sería circular, no un dato nuevo de verdad medido.

### Relación entre la forma física de un deporte y el otro

Es una pregunta genuinamente interesante pero con matices: la parte cardiovascular
(corazón/pulmones) sí transfiere razonablemente bien entre correr y montar en bici,
pero la parte muscular/neuromuscular transfiere mucho menos (correr es de impacto y
trabaja el tren inferior de forma distinta a pedalear). Así que un único "número de
forma física" que sirva para ambos deportes por igual sería una simplificación
excesiva.

Un enfoque más honesto y realista de construir:
- Un indicador de carga de entrenamiento basado solo en pulso (al estilo TRIMP:
  intensidad × duración según tiempo en cada zona de pulso), que por depender solo
  del pulso es automáticamente válido para cualquier deporte sin necesitar ritmo ni
  potencia ni piñón — se podría calcular igual para cada actividad, sea running o
  ciclismo, y verse como una única línea temporal de carga/fatiga combinada.
- Por separado, las gráficas de evolución del baseline por deporte que ya salían
  "gratis" del baseline adaptativo (sección 3) — una para running (p.ej. p50 de
  pulso al ritmo habitual) y otra para ciclismo (p50 de pulso en subida) — puestas
  una al lado de la otra para que el propio usuario pueda ver a ojo si hay
  correlación entre ambas, en vez de que la app afirme una relación causal que no
  puede demostrar con los datos que tiene.
- Herramientas ya existentes en el mercado (TrainingPeaks, Intervals.icu, Golden
  Cheetah) resuelven esto con modelos de carga tipo TRIMP/TSS multideporte —
  confirma que el enfoque de "carga por pulso, cross-sport" es razonable y ya
  probado, no haría falta inventar un modelo fisiológico desde cero.

### La fatiga acumulada como contexto al interpretar una ruta concreta

Idea del usuario: si ha entrenado fuerte de lunes a viernes (aunque sea otro
deporte, p.ej. running), es normal rendir peor el domingo en bici — eso podría
explicar un mal resultado sin que sea un problema real de técnica o de marcha. El
TRIMP diario (sección anterior) no serviría solo para una gráfica de evolución a
largo plazo, sino también como señal a corto plazo para interpretar una salida
concreta.

Esto coincide con un modelo ya establecido en ciencia del deporte (el modelo
Fitness-Fatigue de Banister, la base de lo que TrainingPeaks/Intervals.icu llaman
CTL/ATL/TSB): a partir de la serie diaria de TRIMP se calculan dos acumuladores con
distinta velocidad de decaimiento — uno lento (semanas, "fitness"/forma de fondo) y
uno rápido (días, "fatiga" reciente). Aquí encajarían dos escalas de tiempo
complementarias, no redundantes con lo de la ventana móvil de la sección 3:
- La ventana móvil del baseline (sección 3) capta la tendencia lenta de fondo
  (meses): vas mejorando o empeorando de forma general.
- El acumulador de fatiga (días, ~1 semana) capta el motivo puntual de que ESTA
  salida concreta salga peor de lo normal, aunque la tendencia de fondo sea buena.

Propuesta de cómo usarlo: no cambiar en silencio los umbrales de clasificación de
tramos según la fatiga acumulada (eso podría enmascarar un problema real de técnica
o marcha que simplemente coincide con una semana cargada). Mejor: mantener la
clasificación tal cual sale, y añadir la fatiga acumulada como contexto/anotación en
la propia salida ("esos días llevabas una carga acumulada alta, esto puede explicar
el bajón") para que el usuario tenga la explicación sin que la app la oculte.

Avisos a tener en cuenta: el TRIMP basado en pulso también se ve afectado por calor,
hidratación, sueño, enfermedad, cafeína... no es una medida pura de "dosis de
entrenamiento", así que es una pista, no una certeza. Y como usa solo pulso, suma de
forma natural actividades de cualquier deporte sin trabajo extra — es la misma pieza
que la carga cross-sport de más arriba, reutilizada aquí como señal a corto plazo en
vez de solo como gráfica de tendencia.

### Gráfica de Fitness / Fatiga / Forma (validado por Strava)

El usuario ha enseñado la propia función "Fitness & Freshness" de Strava, que es
literalmente el modelo Fitness-Fatigue de Banister ya implementado por un tercero:
"Fitness" = acumulador lento, "Fatiga" = acumulador rápido, "Forma" = Fitness menos
Fatiga. Confirma que el diseño de esta sección no es una idea rara, es un enfoque ya
probado y reconocible. Un detalle a copiar de cómo lo hace Strava: el desplegable
"Esfuerzo Relativo y potencia" indica que usan pulso (Esfuerzo Relativo, su TRIMP)
por defecto pero cambian a carga basada en potencia cuando el usuario tiene medidor
real — exactamente la misma filosofía de "usa la mejor señal disponible" que ya
aplicamos en todo el resto del motor (piñón > cadencia > pulso, etc.).

Lo que ha pedido el usuario es tener algo parecido, pero explorando una
presentación más fácil de interpretar que la de Strava. El gráfico de Strava es
denso: tres líneas superpuestas, un cono de previsión a la derecha, y tres números
sin unidad intuitiva (32, 38, -6) que solo tienen sentido si ya conoces la
convención CTL/ATL/TSB. Propuesta de simplificación:
- Mostrar por defecto solo **Forma** como un número/indicador principal (con una
  etiqueta cualitativa al lado — algo como "cansado / normal / fresco" según en qué
  franja caiga — en vez de solo un número sin contexto), y dejar Fitness y Fatiga
  como detalle secundario ("ver más") para quien quiera profundizar, en vez de las
  tres líneas compitiendo por la atención desde el principio.
- Complementar (o sustituir en la vista simple) la curva con algo más concreto: un
  listado de los últimos días con su efecto ("Lun: carrera fuerte · Mar: descanso ·
  ... → hoy fatiga alta"), que ata el número abstracto a actividades reales
  reconocibles, en vez de obligar a leer una gráfica.
- Dejar fuera, al menos en una primera versión, el cono de previsión a futuro de
  Strava (proyección si no se entrena más) — es una capa extra que no hace falta
  para el objetivo principal de "explicar por qué esta salida ha ido peor".

Nótese que esta funcionalidad no pide construir nada nuevo aparte de lo ya diseñado
más arriba: "Fitness" es el mismo acumulador lento de la ventana móvil del baseline
(sección 3) y "Fatiga" es el acumulador rápido de TRIMP de esta misma sección — aquí
solo hace falta exponerlos juntos como su propia gráfica/indicador en la interfaz.

## 6. Datos reales de running (Amazfit Balance) — hallazgos

Se ha parseado un FIT real de una carrera del usuario (Amazfit Balance, 24 min,
~4 km) con el mismo parser de bajo nivel ya construido para bici, para ver qué
datos trae de verdad un fichero de running (no solo teoría). Resultado:

**Lo que SÍ viene en cada punto:**
- GPS (lat/lon) — igual que en bici, se decodifica con el mismo código.
- Pulso (campo 3) — igual que en bici.
- Altitud, pero solo en el campo "enhanced_altitude" (78), NO en el campo
  "altitude" (2) que usa el parser de bici hoy. Hay que leer el 78 con
  preferencia sobre el 2 (mismo escalado: valor/5 - 500).
- Velocidad (campo 6) — igual que en bici, m/s = valor/1000.
- **Cadencia de zancada** (campos 4 + 53, o el redundante 52): viene en
  "zancadas/min" (una pierna), no en pasos/min reales — hay que **multiplicar
  por 2** para el dato que la gente conoce como cadencia de correr (se
  verificó con la física real: velocidad ≈ (cadencia×2/60) × longitud de
  zancada, y cuadra con margen de error pequeño).
- **Longitud de zancada** (campo 85, "step_length"): viene en mm ya lista,
  escala/10. Dato extra que en bici no existe.
- **Potencia de carrera estimada** (campo 7, "power"): el reloj calcula y graba
  una potencia instantánea en vatios sin necesidad de ningún sensor externo (a
  diferencia de la bici, donde no hay medidor de potencia). En la carrera
  analizada rondó los 330 W de media. Importante matizarlo (lo señaló el
  usuario): esto también es una **estimación**, no una medición directa de
  fuerza como haría un pedal/potenciómetro de bici o un pod dedicado tipo
  Stryd — el propio Amazfit ofrece soporte para Stryd como opción aparte en
  modelos más nuevos, lo que confirma que la potencia integrada del reloj (la
  que trae este Balance 1) es un cálculo propio del reloj a partir de sus
  sensores (acelerómetro, ritmo GPS, desnivel), no una fuerza medida de
  verdad. La diferencia real frente al caso de Strava/bici de más arriba es
  que aquí el dato SÍ está grabado en el propio fichero desde el dispositivo
  (no es un cálculo posterior de una web), así que sigue sirviendo para
  construir y probar la rama de potencia con datos reales — pero hay que
  tratarla como una estimación razonablemente consistente para comparar
  contigo mismo (baseline personal), no como un vatiaje absoluto fiable al
  milímetro.

**Lo que NO viene (a diferencia de bici) y hay que calcular:**
- **Distancia acumulada**: no hay campo de distancia por punto. Hay que
  derivarla (se probó con dos métodos sobre el fichero real y coinciden con
  ~1.5% de diferencia: integrar velocidad a lo largo del tiempo, o sumar
  distancias GPS punto a punto con la fórmula de haversine que ya usa
  `segment_match.py`). El segundo método (GPS) es más robusto porque no
  depende de que el sensor de velocidad esté bien calibrado.
- **Pendiente**: no viene como campo directo (en bici sí). Se calcula sola en
  cuanto se tiene distancia + altitud (delta altitud / delta distancia), no
  hace falta nada nuevo conceptualmente.

**No aparecen** (al menos en este fichero/reloj) campos de dinámica de carrera
más avanzados como tiempo de contacto con el suelo u oscilación vertical —
solo cadencia, longitud de zancada y potencia estimada, además de lo básico
(GPS, pulso, velocidad, altitud).

**Consecuencia para el parser:** hace falta que sea consciente del deporte
(leído del mensaje `sport`, que además trae directamente el texto `"running"` o
`"cycling"`, no solo un enum) para saber qué campos leer de qué sitio (p.ej.
altitud del 78 en vez del 2) y qué derivar (distancia y pendiente) en vez de
asumir que siempre vienen dados como en los FIT de Karoo.

## 7. Múltiples fuentes de datos (Strava, Hammerhead, Garmin Connect)

Petición del usuario: poder bajar datos de Strava, Hammerhead o Garmin Connect,
eligiendo la fuente y cómo conectarse (tiene cuenta en las tres, se puede probar con
datos reales). Encaja con la idea de "más ciclistas/corredores puedan usar la app"
de la sección 5, pero es una pieza de arquitectura considerable, no un simple añadir
un botón más. Estado real de cada una:

- **Hammerhead**: ya funciona hoy (`hammerhead_sync.py`), descarga los FIT
  originales del Karoo vía su API con OAuth. Es la única fuente que da el detalle
  completo de marchas Di2 (sección "Datos reales" más arriba), porque ese dato solo
  existe en el FIT nativo del dispositivo que lo grabó.
- **Strava**: tiene una API pública (v3) de registro individual, sin necesitar
  aprobación de empresa — cualquiera puede crear una "aplicación" en su cuenta y
  conseguir client_id/secret al momento, así que técnicamente es asequible. Pero
  con una limitación ya confirmada en esta misma conversación: su API **no** da
  acceso al FIT original (el endpoint de descarga original es solo de la web, no de
  la API pública — ver la comprobación que se hizo con el fichero de running), así
  que por API solo se pueden traer los "streams" (series de GPS, altitud, pulso,
  cadencia, y potencia SI el usuario tiene medidor real) en formato JSON propio de
  Strava, no un FIT. Esto significa que Strava como fuente **no puede dar marchas
  Di2** (ni las podría dar aunque el usuario las tuviera), y además hace falta un
  segundo "traductor" de datos distinto del parser de FIT actual, porque el formato
  que devuelve la API no es FIT.
- **Garmin Connect**: aquí está el obstáculo real, aunque con un matiz importante
  (planteado por el usuario): el hecho de que Strava "ingiera" automáticamente las
  actividades subidas por un reloj Garmin demuestra que el mecanismo existe de
  verdad — Garmin tiene una función oficial de "Connected Apps" con la que empuja
  las actividades a Strava en cuanto se suben a Garmin Connect. Pero esa vía es
  precisamente el mismo "Garmin Connect Developer Program" de acceso restringido:
  Strava consiguió esa integración porque la aprobaron como socio (empresa grande,
  caso de uso claro), no porque exista una puerta pública aparte. Comprobado en la
  documentación oficial: hace falta rellenar un formulario y pedir acceso, se
  describe explícitamente como "solo para uso empresarial" ("only for business
  use"), con la posibilidad de que algunas métricas requieran pago de licencia o
  pedido mínimo de dispositivos. Buscando experiencias reales de desarrolladores
  independientes pidiendo acceso para proyectos personales, no hay ningún caso
  confirmado de que lo hayan aprobado — un hilo del propio foro de Garmin con esa
  pregunta exacta se quedó sin respuesta oficial, e incluso alguien apuntó que el
  programa podría estar pensado sobre todo para integración de hardware (otro
  fabricante de wearables), no para apps de lectura de datos personales como esta.
  Conclusión: el mecanismo existe, pero si es viable para un proyecto personal como
  Bikenalysis es genuinamente incierto — vale la pena pedirlo (no cuesta nada
  intentarlo), pero no debería ser el plan A ni bloquear nada mientras se espera
  respuesta.

**Decisión tomada:** se empieza con Hammerhead y Strava como fuentes de datos.
Garmin Connect se deja aparcado por ahora (se puede pedir acceso a su Developer
Program en paralelo, sin que nada dependa de la respuesta).

**Plan práctico realista, en dos velocidades:**
- Sincronización automática por API: Hammerhead (ya la hay) y Strava (viable,
  aunque sin marchas — sirve sobre todo para running y para ciclistas sin Di2).
- Para Garmin Connect si algún día hace falta (y como red de seguridad si la
  solicitud de acceso fallara o tardara): reutilizar la idea que ya habíamos dejado
  apuntada para "amigos sin Hammerhead" — importar un FIT suelto a mano (Garmin
  Connect sí deja exportar cada actividad, o todo el histórico, manualmente desde
  la web/cuenta, aunque no sea automático por API). No es tan cómodo como un
  sincronizado
  automático, pero desbloquea a cualquier usuario de cualquier fuente desde ya,
  sin depender de que Garmin apruebe nada.

**Consecuencia de arquitectura:** hace falta una capa de "fuente de datos"
conectable (cada una con su propio login/autorización y su propia forma de listar y
traer actividades), con un paso de normalización a partir de ahí — todas acaban
convertidas al mismo formato interno de puntos (lat/lon/hr/cadencia/etc.) que ya usa
el resto del motor, sea cual sea el origen. Y una pantalla de "conexiones" donde el
usuario vea qué fuentes tiene enlazadas y pueda añadir/quitar cada una.

## 8. Perfil físico del deportista

Idea del usuario: una zancada de 85 cm no significa lo mismo en un corredor de
1,90 m que en uno de 1,60 m, así que la longitud de zancada (sección 6) debería
compararse en relación a la altura, no en valor absoluto. Correcto — es justo el
mismo principio que ya aplicamos con los baselines personales (comparar contra ti
mismo, no contra un umbral genérico), llevado a un dato que ni siquiera cambia con
el tiempo. Datos de fisonomía que tendría sentido añadir, y por qué:

- **Altura**: normaliza la longitud de zancada (p.ej. zancada como % de la altura,
  en vez de cm sueltos) — la propuesta del usuario. Dato estable, se pide una vez.
- **Peso**: relevante en los dos deportes, no solo en uno. En ciclismo es el
  factor central de vatios/kg para comparar rendimiento en subida (por eso la
  "potencia estimada" de Strava lo pide como input, sección 5/7) — importa incluso
  para un solo usuario a lo largo del tiempo, porque si cambia de peso, el mismo
  esfuerzo (pulso, potencia) rinde distinto en pendiente, algo que ni el baseline
  adaptativo de la sección 3 captaría por sí solo si no sabemos que el peso cambió.
  A diferencia de la altura, el peso puede cambiar, así que tendría sentido
  guardarlo con fecha (un historial, no un único valor fijo) igual que se guardan
  las actividades.
- **Pulso en reposo y pulso máximo**: son los dos datos que de verdad hacen falta
  para calcular bien el TRIMP/carga (sección 5) — la fórmula de Banister usa la
  reserva de pulso (posición del pulso entre reposo y máximo), no el pulso en
  bruto. Sin esto, el indicador de carga/fatiga sería más burdo.

  **Corrección sobre la propuesta anterior** (lo señaló el usuario, con razón): dije
  que el pulso en reposo se podía derivar del valor más bajo sostenido visto en el
  histórico de actividades — eso está mal para el reposo en concreto. Su ejemplo:
  43 ppm en reposo de verdad, un valor que nunca vas a ver en una salida en bici o
  corriendo, porque estar haciendo ejercicio (por suave que sea el calentamiento o
  el enfriamiento) ya te aleja del reposo real. Cualquier "mínimo visto en una
  actividad" sobrestimaría bastante el reposo real de cualquiera con una mínima
  forma física. El pulso máximo es distinto: ahí sí tiene sentido derivarlo de
  datos de actividad, porque un esfuerzo lo bastante duro en una salida o carrera
  puede acercarse de verdad al máximo real (aunque tampoco es perfecto, no todo el
  mundo llega a su máximo en las salidas que graba).

  Por eso el orden correcto es el que propone el usuario: **pedir estos datos al
  usuario primero**, y solo si no los da, recurrir a una inferencia — y aun así, de
  forma distinta según el dato: el pulso máximo sí se puede inferir razonablemente
  del propio histórico de actividades (el más alto visto nunca); el pulso en
  reposo NO debería inferirse de ahí (por el motivo de arriba) — como mucho, de
  datos de pulso continuo de 24h/sueño si la fuente los ofrece algún día (Garmin
  Connect y Zepp/Amazfit sí registran esto, pero es un tipo de dato distinto al de
  las actividades, no viene en los FIT de una salida), y si no hay ni eso, mejor
  dejarlo vacío/sin usar esa parte del cálculo que inventar un número con un método
  que sabemos que da mal.
- **Longitud de pierna** (opcional, más preciso que la altura para lo de la
  zancada): la altura es un buen valor por defecto porque es fácil de dar y
  suficientemente buena aproximación; la longitud de pierna real (p.ej. entrepierna)
  afina más pero ya es un dato que la mayoría no tiene a mano sin medirse. Se
  puede dejar como campo opcional avanzado, con la altura como valor por defecto.
- **Edad**: al igual que altura/peso, no se puede inferir de ningún dato de
  actividad — se pide directamente al usuario, campo opcional. Útil sobre todo
  para el problema de "usuario nuevo sin histórico" (sección 3): con cero salidas
  propias no hay baseline personal todavía, y una fórmula genérica de pulso máximo
  estimado por edad podría servir de arranque provisional hasta que se acumulen
  datos reales — un parche para el arranque en frío, no el corazón del sistema
  (que sigue siendo comparar a cada uno contra sí mismo).
- **Sexo**: mismo caso que la edad (se pide directamente, no se infiere), con el
  mismo uso limitado de arranque en frío — algunas fórmulas genéricas de pulso
  máximo/zonas lo usan como variable. Campo opcional, no imprescindible.

**Dónde encaja:** un perfil del deportista en los settings (sección 4), que la app
pida directamente al usuario (altura, peso, pulso reposo/máximo) al configurarla o
la primera vez que hagan falta; solo si el usuario no los rellena, se recurre a
inferirlos de los datos donde tenga sentido (pulso máximo sí, pulso en reposo con
matices como se explica arriba, altura/peso no se pueden inferir en absoluto y
tienen que venir del usuario si se quieren usar). Peso con fecha como historial;
longitud de pierna/edad/sexo como campos opcionales de segundo orden.

## 9. Pendiente para cuando se retome el código

- Reescribir la segmentación de `interesting_points.py` (hoy punto a punto) para
  trabajar por tramos según las reglas de la sección 1.
- Extender `baselines.py` con las variantes de bajada y con el cálculo por ventana
  móvil (en vez de histórico completo fijo).
- Añadir la rama de potencia al formato de datos y al clasificador (sin validar con
  datos reales hasta que exista un FIT con potencia).
- Añadir la categoría de tramos "bien ejecutados".
- Exponer los tres settings de la sección 4 en la interfaz (hoy no hay pantalla de
  settings; hay que crearla).
- Añadir sección de running (ya con datos reales verificados, sección 6): hacer el
  parser consciente del deporte (leer el mensaje `sport`), leer altitud del campo
  78 en vez del 2 para running, derivar distancia (GPS/haversine) y pendiente
  (altitud/distancia) ya que no vienen dadas, doblar la cadencia de zancada para
  mostrarla como pasos/min, y construir de una vez la rama de potencia (sección 5)
  para running, que aquí sí se puede validar con datos reales.
- El indicador de carga cross-sport y el de fatiga a corto plazo (sección 5)
  dependen de tener ya actividades de los dos deportes en el dataset — se hacen
  después de tener running funcionando.
- Selector de deporte (ciclismo/running) en la interfaz.
- Capa de fuentes de datos conectables (sección 7): empezar por el conector de
  Strava (API individual, viable ya), dejar Garmin Connect para cuando/si se
  aprueba el acceso (mientras tanto, importación manual de FIT como red de
  seguridad para Garmin y para cualquier otra fuente).
- Gráfica de Fitness/Fatiga/Forma simplificada (sección 5), reutilizando los
  acumuladores de baseline y TRIMP que no son trabajo nuevo aparte.
- Perfil físico del deportista (sección 8): pantalla que pida altura, peso y
  pulso reposo/máximo al usuario directamente; inferencia de datos solo como
  respaldo si no los da (y solo para pulso máximo y, con matices, pulso en
  reposo — altura y peso no se pueden inferir). Peso con historial por fecha;
  longitud de pierna/edad/sexo como opcionales de segundo orden.
