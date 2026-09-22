# Operación de MAGI

Qué hacer cuando algo se rompe. Cada alerta de `healthcheck.py` termina con
el ancla de su sección acá, así que si el toast dice `docs/operacion.md#postgres-caido`,
esta es la página y esa es la sección.

Dos comandos antes que nada:

```bash
# ¿puede correr MAGI en esta máquina? (instalación: venv, base, cabezas, permisos)
debate-mcp/.venv/Scripts/python.exe debate-mcp/doctor.py

# ¿está sano el sistema EN MARCHA? (Postgres, relay, decisiones, grafo, asientos)
debate-mcp/.venv/Scripts/python.exe debate-mcp/healthcheck.py
```

`doctor.py` es para instalar y reparar; `healthcheck.py` es para el día a día
y corre solo cada 15 minutos. El botón **Diagnóstico** de la UI muestra lo
mismo que `doctor.py` sin abrir una terminal.

---

## postgres-caido

**Síntoma**: `CRIT postgres inalcanzable`, la UI no carga decisiones, el
heartbeat del relay dice `pg_ok: false`.

La causa más común en Windows es que alguien corrió `bin/stop-magi.bat`, que
también apaga Postgres, o que la laptop se suspendió. Levantarlo:

```powershell
powershell -ExecutionPolicy Bypass -File debate-mcp\bin\start-magi.ps1 -NoBrowser
```

Ese script arranca Postgres si hace falta, aplica migraciones pendientes y
levanta relay y UI sólo si no están corriendo. Si Postgres no arranca, el log
está en `experiments/pg/pg.log`.

Nada se pierde con la base caída: las decisiones viven en Postgres y los
turnos en vuelo fallan con ERROR, que se reintenta desde la UI
(ver [cabeza en ERROR](#asiento-degradado)).

## relay-congelado

**Síntoma**: `CRIT relay congelado` o `WARN relay lento o trabado`. El
heartbeat (`debate-mcp/logs/relay_heartbeat.json`) tiene minutos de viejo.

1. Mirá `debate-mcp/logs/relay.stderr.log`: las últimas líneas dicen en qué
   turno se quedó.
2. Si hay un turno en vuelo desde hace más que su `timeout_secs`, el proceso
   de la cabeza se colgó: matalo y el relay sigue solo.
3. Reinicio sólo del relay (deja Postgres y la UI en pie, así que **no se
   pierden las pestañas abiertas**):

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'relay\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
powershell -ExecutionPolicy Bypass -File debate-mcp\bin\start-magi.ps1 -NoBrowser
```

Nunca uses `stop-magi.bat` para esto: apaga Postgres y corta cualquier
corrida manual en curso (`council_synthesis.py --retry`, `acceptance_run.py`,
`persona_ab.py`).

## decision-trabada

**Síntoma**: `CRIT decisión trabada` o `WARN decisión lenta en cerrar`.

Una decisión abierta espera votos. Las causas reales, en orden de frecuencia:

- **Una cabeza en ERROR** que nadie reintentó → ver [abajo](#asiento-degradado).
  Desde el 2026-09-21, si los asientos que faltan están en ERROR y los que
  votaron son mayoría y coinciden, la ronda cierra sola **degradada**.
- **Tope de rondas** (`capped_threads` en el heartbeat): el consejo agotó su
  presupuesto. Cerrala vos con **close with my ruling** o mandá `seguí` para
  otra ronda con contexto.
- **Split**: tres posiciones distintas. La UI pide arbitraje.

Para abandonar una decisión sin arbitrar, el botón **Abort decision**.

## asiento-degradado

**Síntoma**: `WARN asiento degradado: <asiento>/<turno> N/M fallos`, o una
cabeza en rojo con **ERROR** en la tarjeta.

Un turno fallido nunca se convierte en voto ni se reintenta solo: queda
anotado en el dossier y espera decisión humana.

1. Abrí el error (clic en la cabeza roja) y leé el mensaje.
2. Causas típicas: límite de sesión del CLI (esperar al reset), binario
   movido (`doctor.py` lo dice), o el modelo devolvió texto sin el tag
   `POSITION:`.
3. Arreglada la causa, **Reintentar cabezas fallidas** en la UI.
4. Tres turnos de voto fallidos seguidos en el mismo día ponen al asiento en
   **cuarentena**: las decisiones nuevas se abren sin él hasta que un turno
   suyo salga bien. Se anota en el journal y lo reporta el healthcheck.

Los detalles por asiento y tipo de turno están en
`metrics.py --days 7` (ver [métricas](#leer-las-metricas)).

## api-y-cuarentena

**Síntoma**: `WARN api: <asiento>: falta la variable …` o `<asiento> en
cuarentena`.

Los asientos `type: "api"` necesitan `model`, el proveedor y su clave en la
variable de entorno que diga `api_key_env`. Sin clave, el asiento no se
sienta (decisión degradada, no colgada). Para agregarla en Windows:

```powershell
[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', 'sk-ant-...', 'User')
```

y reiniciá el relay para que la vea. 429 y 5xx se reintentan solos tres veces
con backoff; un 401 falla de una y es siempre configuración.

## grafo-viejo

**Síntoma**: `CRIT grafo stale` o `WARN <fuente> sin correr desde …`.

El grafo de memoria (`memory-graph/memory.db`) se reingesta con:

```bash
bash memory-graph/refresh.sh
```

En Windows lo hace la tarea `ClaMi-memory-refresh` cada hora. Si la tarea no
existe, `doctor.py` lo avisa. Un grafo viejo no rompe nada: el consejo
delibera con menos memoria, y las fuentes que faltan se nombran en la alerta.

## merge-pendiente

**Síntoma**: en el journal, `MERGE PENDIENTE` o `MERGE DETENIDO`.

- **PENDIENTE** significa que la revisión no aprobó por unanimidad. Si aprobó
  2 de 3, la UI ofrece **Mergear con 2/3**: tu autorización queda registrada
  como arbitraje y el relay integra con los mismos chequeos que un merge
  unánime.
- **DETENIDO** significa que Git se negó: la rama base cambió, hay archivos
  sin commitear, o el worktree se tocó a mano. El mensaje dice cuál. El
  trabajo **no se pierde**: vive en la rama `magi/d<n>` y su worktree, bajo
  `<git-common-dir>/magi-worktrees/`. Resolvé el estado del repo y volvé a
  pedir el merge, o abrí un plan nuevo sobre la base actual.

Después de un merge exitoso el ejecutor borra el worktree del plan, el de
integración y la rama. Si quedaron worktrees viejos de fallas anteriores:

```bash
git worktree list                     # ver qué hay
git worktree remove --force <ruta>    # borrar uno
git worktree prune
```

## reiniciar-sin-perder-pestanas

La UI guarda su token de sesión en `debate-mcp/logs/ui_token` y las pestañas
abiertas lo revalidan solas: **reiniciar no cierra sesión**. Si una pestaña
quedó con un token viejo (de antes de que el token se persistiera), se
recarga sola al detectarlo.

- Reiniciar **todo**: `bin\stop-magi.bat` y después `bin\start-magi.ps1`.
  Apaga Postgres: no lo hagas con corridas manuales en curso.
- Reiniciar **sólo el relay** (lo habitual tras cambiar código): ver
  [relay congelado](#relay-congelado).
- Los procesos se crean vía WMI, así que no son hijos de la consola: cerrar
  la terminal no los mata. Tras un reinicio de Windows, la entrada
  `ClaMi-magi.cmd` de la carpeta Inicio los vuelve a levantar.

## leer-las-metricas

```bash
debate-mcp/.venv/Scripts/python.exe debate-mcp/metrics.py --days 7
debate-mcp/.venv/Scripts/python.exe debate-mcp/metrics.py --quality --days 7
```

- Sin `--quality`: una fila por asiento y tipo de turno, con p50/p95/max de
  duración, tasa de error, timeouts, tokens aproximados y **costo** real de
  los asientos que reportan uso (claude en modo `claude-json`, codex en
  `codex-jsonl`, y cualquier asiento API con `pricing`). Kimi no reporta uso.
- Con `--quality`: el panel semanal: decisiones cerradas por veredicto,
  cuántas tienen resultado reportado, cuánta memoria se calificó, tiempo y
  tokens por decisión, turnos de cabeza fallidos. Corre solo los lunes a las
  9:00 (tarea `ClaMi-quality`) con un toast.

Las dos metas del plan de madurez son ≥ 50 % de decisiones cerradas con
resultado y ≥ 30 % de la memoria calificada. Se cumplen usando la línea
«¿Cómo salió la #n?» y los 👍/👎 de la tarjeta, no tocando código.

## leer-el-healthcheck

```bash
debate-mcp/.venv/Scripts/python.exe debate-mcp/healthcheck.py           # reporte
debate-mcp/.venv/Scripts/python.exe debate-mcp/healthcheck.py --notify --quiet
```

Seis chequeos: `postgres`, `relay`, `decisions`, `memory-graph`, `seats` y
`api`. Código de salida 0 si todo bien, 1 con avisos, 2 con algo crítico.
`--quiet` sólo imprime cuando algo falla, que es como corre la tarea
programada cada 15 minutos; `--notify` agrega el toast.

## backups

Lo único irreemplazable es Postgres: las decisiones, el journal, los votos y
los resultados. El grafo de memoria se reconstruye desde cero con
`refresh.sh`, y los worktrees son temporales.

```bash
pg_dump -h 127.0.0.1 debate > backup-$(date +%Y%m%d).sql
```
