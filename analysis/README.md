# Bikenalysis

Una sola app para todo: sincronizar las actividades nuevas desde Hammerhead
y analizarlas (mapa, puntos interesantes, comparacion de tramos). Todo corre
en local, solo con la libreria estandar de Python (nada de `pip install`)
salvo el mapa, que carga Leaflet desde un CDN publico la primera vez que
abres la pagina (necesitas conexion a internet para eso, igual que para ver
el mapa de fondo de OpenStreetMap) -- y salvo `pywebview`, que se usa para
mostrar la app en su propia ventana en vez de como una pestana de navegador
(ver mas abajo).

## Uso normal: `Bikenalysis.exe`

La forma pensada para el dia a dia es compilar un `.exe` una sola vez
(`build_exe.bat`, ver abajo) y a partir de ahi solo hace falta doble clic en
`dist\Bikenalysis.exe` -- se abre en su propia ventana (no una pagina web),
sin consola ni terminal de por medio, y no hace falta tener Python instalado
para usarlo (el `.exe` lo lleva todo dentro). Los datos (`fit_files\`,
`data\`, `tokens.json`...) se guardan junto al `.exe`, asi que puedes mover
ese fichero donde quieras (escritorio, otra carpeta) y se lleva sus datos
consigo si mueves la carpeta entera.

Para (re)generar el `.exe` tras cambiar el codigo:

```
build_exe.bat
```

Esto instala `pywebview` + `pyinstaller` si hace falta y deja el resultado en
`dist\Bikenalysis.exe`.

## Uso en desarrollo (desde el codigo fuente)

```
cd analysis
python app.py --build
```

Esto genera el dataset (parsea todos los `.fit` que ya tengas en
`../fit_files/`, calcula tus baselines personales y detecta puntos
interesantes) y abre una ventana propia (o el navegador si `pywebview` no
esta instalado). Tarda un minuto o dos la primera vez con las ~116
actividades actuales; las siguientes veces basta con:

```
python app.py
```

Otras banderas utiles: `--browser` (fuerza pestana de navegador en vez de
ventana propia), `--no-browser` (no abre nada, solo sirve la API, util para
depurar), `--port 9000`.

## Sincronizar actividades nuevas

Con la app abierta, el boton **🔄 Sincronizar** de arriba a la derecha
descarga las actividades nuevas de Hammerhead (usando el mismo `.env` /
`tokens.json` de siempre) y actualiza el dataset automaticamente -- ya no
hace falta lanzar `hammerhead_sync.py` aparte ni volver a pasar `--build` a
mano. Solo se re-parsean los `.fit` nuevos (el resto se reutiliza tal cual),
asi que sincronizar es rapido aunque ya tengas cientos de salidas.

`hammerhead_sync.py` sigue existiendo y funcionando igual por linea de
comandos (`python hammerhead_sync.py sync`), por si algun dia prefieres
lanzarlo asi -- pero para el uso normal ya no hace falta. La unica excepcion
es la autorizacion inicial (`python hammerhead_sync.py auth`, que abre el
navegador para dar permiso a la app en Hammerhead): eso ya lo hiciste una
vez y solo tocaria repetirlo si algun dia revocas el acceso.

## Que un amigo use su propia copia (multiusuario)

Bikenalysis no es un servicio compartido: cada instalacion (cada carpeta con
su propio `Bikenalysis.exe`) tiene sus propios datos y credenciales,
completamente aislados de las demas. Para que otra persona la use con sus
propias actividades:

1. Que copie la carpeta entera (o compile su propio `.exe` con
   `build_exe.bat`) a un sitio nuevo -- **no** dentro de la misma carpeta que
   ya tiene tus datos.
2. Que rellene su propio `.env` (copiar `.env.example` a `.env`): necesita un
   Client ID/Secret de Hammerhead (dashboard.hammerhead.io -> Ajustes -> API,
   ver mas abajo) y/o de Strava (strava.com/settings/api) -- son gratis y
   tardan 2 minutos en crearse, cada uno con su propia cuenta.
3. Que abra su copia de la app, vaya a **Ajustes -> Conexiones**, y pulse
   **Conectar** en Hammerhead y/o Strava -- se abre su navegador para que
   inicie sesion con su propia cuenta y apruebe el acceso. A partir de ahi
   "Sincronizar" descarga solo sus actividades.

No hace falta compartir tokens ni ficheros de datos entre instalaciones --
cada persona autoriza la app con su propia cuenta y sus datos se quedan solo
en su carpeta.

## Que hace cada cosa

- `fit_parser.py` -- parser de `.fit` (sin dependencias). Importante: los
  ficheros de este Karoo usan **big-endian** para el mensaje `record`
  (posicion, altitud, velocidad...) pero el campo de cambio de marcha del
  mensaje `event` va en el orden de bytes del fichero independientemente de
  eso -- los dos bugs que costo encontrar estan documentados en el propio
  codigo.
- `build_dataset.py` -- parsea todas las actividades y guarda
  `data/summaries.json` + `data/records/<id>.json.gz` (por actividad).
- `baselines.py` -- percentiles personales (pulso, cadencia, pendiente,
  piñon) generales y "mientras subes", para calibrar los umbrales del motor
  de puntos interesantes a como ruedas tu, no a valores inventados.
- `interesting_points.py` -- detecta tramos de "atrancado" (pendiente +
  piñon pequeño + pulso alto + cadencia baja) y variantes mas debiles cuando
  falta cadencia o datos de marcha Di2.
- `route_index.py` / `segment_match.py` -- comparacion de dos actividades:
  encuentra que tramos de carretera tienen en comun (aunque el resto de la
  ruta sea distinta) y compara velocidad/pulso/cadencia/desnivel en cada uno.
  `segment_match.py` tambien busca, para un tramo interesante concreto,
  otras veces que se haya pasado por el mismo sitio ("Otras veces por aquí").
- `../hammerhead_sync.py` / `strava_sync.py` -- conectores de cada fuente de
  datos. Hammerhead da el FIT original completo (unica fuente con marchas
  Di2); Strava no da FIT, solo "streams" ya calculados (sin marchas, pero
  incluye tipos de actividad mas finos -- carretera/montaña/gravel/running).
  Las dos conviven en el mismo dataset (`strava_sync.py` descarta duplicados
  cuando la misma salida esta en las dos fuentes) y se conectan desde
  Ajustes -> Conexiones en la app, no hace falta usar la consola.
- `app.py` + `static/` -- servidor local (solo stdlib) y la pagina
  (lista de actividades, detalle con mapa + grafica + puntos interesantes,
  comparador). Por defecto la muestra en una ventana propia con `pywebview`
  en vez de en el navegador.
- `../build_exe.bat` -- genera `dist/Bikenalysis.exe` (PyInstaller) para el
  uso normal de doble clic, sin terminal ni Python instalado.

## Aviso importante sobre los datos

A dia de hoy, de las actividades descargadas solo las mas recientes llevan
datos de cambios de marcha Di2 (desde que se configuro el puente Ki2) y unas
pocas mas antiguas llevan cadencia. El motor de puntos interesantes usa la
mejor combinacion de señales disponible en cada actividad -- las mas
completas (marcha + cadencia + pulso + pendiente) dan el diagnostico mas
fino, el resto usan una version mas conservadora basada solo en pulso y
pendiente. Esto mejorara solo con pedalear mas con el Ki2 activo: no hace
falta volver a tocar nada, `python app.py --build` lo recalculara todo con
mas datos cuando toque.
