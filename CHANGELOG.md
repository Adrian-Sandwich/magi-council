# Changelog

Formato: lo que cambió para quien **usa** MAGI, no un volcado de commits.
El detalle línea por línea está en `git log`.

## v0.1 — 2026-09-21

Primera versión etiquetada. El consejo delibera, ejecuta planes en un
worktree aislado, recuerda lo que pasó y se puede instalar y operar sin
leer el código. 103 commits desde el 2026-08-07.

### El consejo

- **Motor de decisiones MAGI** sobre Postgres: tres asientos (melchior,
  balthasar, casper), protocolos `vote`, `critique` y `adaptive`, mayoría
  2/3 con minority report completo, y split a arbitraje humano cuando no hay
  acuerdo. Tres posiciones distintas nunca se resuelven solas.
- **Personas con sesgo de comportamiento** (`personas.py`, modo `behavioral`
  por defecto): cada asiento decide desde un eje distinto. Medido con
  `persona_ab.py` sobre 8 decisiones: 83 / 87 / 88 % de diversidad de
  argumentos según el modo, sin mover el voto fuera del ruido.
- **Cabezas intercambiables** en `heads.json`: CLI con MCP, CLI con el
  journal inlineado (codex, kimi) o asientos API — Anthropic, OpenAI,
  Moonshot y cualquier endpoint OpenAI-compatible local. Un asiento sin
  binario o sin clave no cuelga al consejo: la decisión se abre degradada.
- **Síntesis conjunta**: una cabeza redacta la respuesta y todas las demás
  la revisan por fidelidad a las fuentes, con reglas editoriales fijas
  (voz del consejo, lenguaje llano, tope de palabras y de condiciones).
- **Preguntas sin repositorio**: filosofía, documentos, decisiones de
  producto. Las cabezas no salen a leer archivos que no vienen al caso.

### Ejecución

- **Modo producción sin interruptor**: una conversación aprobada evoluciona a
  plan ejecutable con «vamos con tu plan», «arreglalo» o «aplicá la
  propuesta».
- **Aislamiento real**: rama `magi/d<n>` en su propio worktree desde el
  commit base guardado; revisión sobre el diff exacto; merge `--no-ff`
  preparado en un worktree de integración y `--ff-only` sobre tu rama, que
  nunca se cambia sola. Al integrar se podan worktrees y rama.
- **Merge con 2/3**: si la revisión aprueba 2 de 3, tu autorización queda
  registrada como arbitraje y el relay integra con los mismos chequeos.
- **Fallas explicadas**: cada ejecución fallida trae su causa (`timeout`,
  `crash`, `error`, `sin_cambios`, `sin_commit`, `entorno`) y las últimas
  40 líneas del log en el journal.
- **Corrida de aceptación**: 10 planes reales, 10 llegaron a revisión y 10 se
  integraron, entre 151 s y 334 s cada uno (`docs/executor-runs.md`).

### Memoria

- **Grafo de memoria** (SQLite) alimentado por conversaciones de Claude y
  Kimi, el tablero, los documentos y el código, con ingesta incremental.
- **Bloque de memoria selectivo** por decisión: a lo sumo 5 dossiers y 4 500
  caracteres, sólo fuentes que comparten hilo, título, términos o similitud
  semántica.
- **Brief del repositorio** desde el grafo de código en la ronda 1: tamaño,
  carpetas, entradas HTTP, archivos más referenciados y más recientes.
- **Búsqueda semántica local** (multilingüe, sin API) con vectores float32.
- **Evaluación con follow-ups reales**: recall leave-one-out y precisión por
  fuente a partir de tus 👍/👎 (`memory-graph/eval_retrieval.py`).

### Interfaz

- Consola web estilo NERV: una caja de texto, dos modos (consejo y chat),
  historial por chips de color, síntesis antes del journal, sonido
  industrial opcional y vista móvil.
- **Resultados**: «¿Cómo salió?» con calificación de un clic al abrir la
  pregunta siguiente; 👍/👎 sobre la memoria que vio el consejo.
- **Diagnóstico** en la barra: corre `doctor.py` y muestra cada remedio.
- Token de sesión persistente: reiniciar no cierra las pestañas abiertas.

### Operación

- **Instalación en un paso**: `install.ps1` (Windows, con Postgres portátil
  opcional) e `install.sh` (macOS/Linux). Idempotentes.
- **`doctor.py`**: ocho chequeos de instalación, cada problema con su línea
  de remedio. **`healthcheck.py`**: seis chequeos del sistema en marcha, cada
  alerta enlazada a su sección de `docs/operacion.md`.
- **`metrics.py`**: latencia, errores y timeouts por asiento y turno, tokens
  aproximados y **costo real** de las cabezas que reportan uso (claude y
  codex en modo JSON, asientos API con precios). `--quality` imprime el panel
  semanal, agendado los lunes con toast.
- **Tareas de Windows**: arranque al iniciar sesión, healthcheck cada 15
  minutos, refresh del grafo por hora, panel semanal. Los procesos se crean
  vía WMI: cerrar la terminal no los mata.
- **Manual de operación** (`docs/operacion.md`): Postgres caído, relay
  congelado, decisión trabada, cabeza en ERROR, grafo viejo, merge pendiente,
  cómo reiniciar sin perder pestañas y cómo leer las métricas.

### Pruebas

- Suite con repositorios Git temporales, Postgres en tablas `pg_temp` y
  pruebas de navegador con Playwright.
- **CI** en Ubuntu (Postgres 16 + Chromium) y Windows desde el 2026-09-18.

### Lo que todavía no está

- Segunda corrida de personas con n ≥ 20 y las tres cabezas reales.
- Dos semanas de uso para las metas de cobertura: ≥ 50 % de decisiones con
  resultado reportado, ≥ 30 % de memoria calificada.
- Instalación verificada en una máquina limpia (los instaladores se probaron
  sobre una instalación existente).
- macOS: los `launchd/*.plist` son plantillas sin probar en esta versión.
