# Plan de madurez — de prototipo del autor a herramienta que otro pueda operar

Estado al 2026-09-18: motor de decisión maduro y testeado (~370 tests en verde,
43 decisiones reales), modo producción diseñado pero con 4 de 7 ejecuciones
fallidas, memoria completa sin prueba de valor, UI usable, operación recién
instrumentada, **sin CI**, proveedores por scraping de CLI, sólo local.

Cinco frentes, en el orden en que se desbloquean unos a otros. Cada tarea trae
un criterio de aceptación verificable; "hecho" es que el criterio se cumple, no
que el código exista.

## 1. CI — que la suite corra fuera de esta máquina (1–2 días)

Hoy la única red de seguridad es correr `pytest` a mano en Windows.

| # | Tarea | Aceptación |
|---|---|---|
| 1.1 | `.github/workflows/ci.yml`: matriz `windows-latest` + `ubuntu-latest`, Python 3.14; instala `requirements.txt` + `requirements-dev.txt`; corre `pytest -q`. Los canarios (`test_canary_*`) y los tests con `CLAMI_BROWSER_PATH` se saltan solos; los de Postgres, ver 1.2. | Badge verde en `main`; un PR con un test roto queda rojo. |
| 1.2 | Postgres en CI: servicio `postgres:16` en el job de Ubuntu con `CLAMI_TEST_POSTGRES_DSN`, corriendo `schema/migrate.py` antes. En Windows se saltan (ya lo hacen). | `test_outcomes` y `test_production_git` (parte Postgres) corren en Ubuntu. |
| 1.3 | Navegador en CI: `playwright install chromium` en Ubuntu y `CLAMI_BROWSER_PATH` apuntando a él. | Los 17 tests de `test_ux_browser.py` corren en CI. |
| 1.4 | `ruff` (sólo errores: F, E9) como paso previo. | Un import roto o una variable sin definir falla antes de los tests. |
| 1.5 | Rama protegida: `main` exige CI verde. | Un push directo con la suite rota es rechazado. |

Dependencias: ninguna. Es lo primero porque todo lo demás se mide con esto.

## 2. Ejecutor confiable — demostrar el ciclo plan → diff → merge (3–5 días)

Las 4 fallas registradas están en la #28 y son de plomería, no del modelo:

- **WinError 32** ×2: `tempfile.TemporaryDirectory` en `relay.py:1572` borra el
  directorio del prompt mientras el proceso hijo (codex) todavía lo tiene
  abierto — carrera propia de Windows.
- **rc=4294967295** (−1): el ejecutor murió sin salida; hoy no se distingue de
  un rechazo del modelo.
- **"dejó cambios sin commit"**: el ejecutor terminó sin `git commit`; la regla
  es correcta, pero no se le dice al ejecutor cómo confirmar ni se verifica
  antes de matar el turno.
- Además: 3 revisiones `MERGE PENDIENTE` por no unanimidad y un `MERGE DETENIDO`
  por identidad Git (ya corregido).

| # | Tarea | Aceptación |
|---|---|---|
| 2.1 | Directorio de trabajo del ejecutor persistente por decisión (`logs/executor/d<n>/`), no un tempdir; limpieza al cerrar la decisión. Mismo criterio que se aplicó a la síntesis. | Test que simula un hijo que retiene el archivo; 0 `WinError 32` en 10 ejecuciones reales. |
| 2.2 | Salida del ejecutor siempre capturada y clasificada: `rc` del proceso, últimas 40 líneas en el journal, causa etiquetada (`timeout`, `crash`, `sin_commit`, `modelo_rechazó`). | `metrics.py` y el journal muestran la causa; ninguna falla queda como "rc=−1" a secas. |
| 2.3 | Contrato de cierre para el ejecutor: el prompt exige `git add -A && git commit` con mensaje dado, y el relay verifica `git status --porcelain` vacío **antes** de declarar fallo; si hay cambios sin commit, el relay los commitea con mensaje `magi: cambios del ejecutor sin confirmar` y abre la revisión igual. | Test de regresión; la falla "dejó cambios sin commit" desaparece del catálogo. |
| 2.4 | Reintento automático UNA vez para `crash`/`timeout` de arranque (misma política que las cabezas); las demás causas siguen esperando al operador. | Test; el turno en ERROR sólo aparece por causas de contenido. |
| 2.5 | Corrida de aceptación: 10 planes reales pequeños (tests que faltan, docs, refactors acotados) en un repo de prueba y en este, con `metrics.py` midiendo. | ≥ 8/10 llegan a revisión; ≥ 5/10 se mergean sin intervención; las causas de los que no se registran en `docs/executor-runs.md`. |
| 2.6 | Revisión no unánime: hoy queda `MERGE PENDIENTE` sin camino claro. Añadir en la UI la acción «Mergear con 2/3» (arbitraje explícito del operador, registrado en el journal). | Test de UI + endpoint; la #28 se podría haber cerrado desde la pantalla. |

Dependencias: 1 (los tests de 2.x corren en CI). 2.5 necesita 2.1–2.4.

## 3. Lazo de calidad — que «¿sirve?» tenga datos (2–3 días + uso)

Existen outcomes (`¿Cómo salió?`) y etiquetas de memoria (👍/👎) pero nadie las
usa: 0 outcomes desde el paso 5, 0 etiquetas. Si no se recogen, la memoria y
las personas se seguirán ajustando a ojo.

| # | Tarea | Aceptación |
|---|---|---|
| 3.1 | Pedir el outcome en el momento correcto: al abrir una decisión nueva, si la anterior cerrada del mismo repo no tiene outcome, la UI pregunta en una línea («¿Cómo salió la #41?» con tres botones) antes de enviar. | Test de UI; en dos semanas ≥ 50 % de las decisiones cerradas tienen outcome. |
| 3.2 | 👍/👎 con un clic desde la tarjeta, sin desplegar nada, y registro de "sin calificar" como estado explícito. | ≥ 30 % de las decisiones con memoria calificadas en dos semanas. |
| 3.3 | `eval_retrieval.py` usa las etiquetas: precisión de las fuentes mostradas (útil / no útil) además del recall leave-one-out. | El informe muestra ambas métricas con n. |
| 3.4 | Panel semanal (`metrics.py --quality`): outcomes por resultado, tasa de "no sirvió", tiempo por decisión, tokens por decisión, fallos de cabeza. | Un comando imprime el resumen; se agenda semanal con toast. |
| 3.5 | Segunda corrida de `persona_ab.py` con n ≥ 20 decisiones y las tres cabezas reales (no sólo Claude), para separar proveedor de persona. | Tabla en `docs/personas.md`; decisión documentada de mantener o cambiar el modo. |

Dependencias: 1. Se puede hacer en paralelo con 2.

## 4. Proveedores por API con costo y reintentos (3–4 días)

Hoy los tres asientos son CLIs con parseo de stdout: crashes de arranque,
límites de sesión que tiran 29 votos, cero información de costo. Los CLIs
seguirán siendo útiles para investigar repos con herramientas; para votar,
revisar y sintetizar basta una API.

| # | Tarea | Aceptación |
|---|---|---|
| 4.1 | `apihead.py` ya habla OpenAI-compatible; añadir proveedor Anthropic (Messages API, `claude-*`) y Moonshot (OpenAI-compatible) con `type: "api"` y clave por variable de entorno. | Un asiento `api` con Claude vota una decisión real de punta a punta. |
| 4.2 | Uso y costo: cada turno API registra `input_tokens`/`output_tokens` reales y el costo estimado con una tabla de precios en `heads.json`; `metrics.py` los muestra junto a los aproximados. | Columna `costo` con datos reales para asientos API. |
| 4.3 | Reintentos con backoff para 429/5xx/timeout (3 intentos), respetando `retry-after`. | Test con servidor falso; un 429 no marca ERROR. |
| 4.4 | Modo mixto recomendado y documentado: una cabeza CLI con herramientas (investiga el repo) + dos API (votan sobre el journal y el mapa del repo). Medir con `metrics.py` latencia y tokens contra el modo actual. | Tabla comparativa en el README; default elegido con datos. |
| 4.5 | Fallback: si un asiento API falla 3 veces seguidas en el día, `healthcheck` lo marca y el relay lo salta (decisión degradada, no colgada). | Test; el consejo cierra con 2 cabezas y `degraded: true`. |

Dependencias: 1 y 3.4 (para medir). Independiente de 2.

## 5. Instalación en un paso y guía de operación (2 días)

| # | Tarea | Aceptación |
|---|---|---|
| 5.1 | `install.ps1` / `install.sh`: crea el venv, instala requisitos, levanta Postgres portátil o usa `DEBATE_CONNINFO`, migra, registra las tareas programadas (refresh + healthcheck), escribe un `heads.json` de ejemplo y arranca. | En una máquina limpia (VM), de clonar a ver la UI en < 10 min siguiendo sólo el README. |
| 5.2 | `doctor.py`: comprueba binarios de los asientos, autenticación de cada CLI/API, Postgres, permisos de escritura, modelo semántico; imprime qué falta y cómo arreglarlo. | Cada error tiene una línea de remedio; se ejecuta al inicio y desde la UI. |
| 5.3 | `docs/operacion.md`: qué hacer cuando una cabeza está en ERROR, cuando Postgres cae, cuando el grafo envejece, cuando un merge queda pendiente, cómo reiniciar sin perder pestañas, cómo leer `metrics.py` y `healthcheck.py`. | Cada alerta del healthcheck enlaza a su sección. |
| 5.4 | macOS: probar `launchd/` de nuevo o retirarlo del README hasta que se pruebe. | Lo que dice el README se ejecutó en la plataforma que nombra. |
| 5.5 | Versionado: `CHANGELOG.md` a partir de los commits (hay 100+ con buen mensaje) y etiqueta `v0.1`. | `git tag v0.1` con notas. |

Dependencias: 1. Se puede empezar en paralelo; 5.1 conviene después de 4 para que el ejemplo de `heads.json` incluya asientos API.

## Orden propuesto y calendario

```
semana 1  ── 1 CI (1.1–1.5) ──┬── 2.1–2.4 ejecutor (plomería)
                              └── 3.1–3.2 recoger outcomes y etiquetas (empieza a acumular datos)
semana 2  ── 2.5 corrida de aceptación del ejecutor ── 4.1–4.3 API + costo
semana 3  ── 4.4 modo mixto medido ── 3.3–3.5 calidad con datos ── 2.6
semana 4  ── 5 instalación, doctor, guía, changelog, v0.1
```

Qué NO está en el plan a propósito: multiusuario/autenticación remota (el
sistema es local por diseño), FTS/ANN en el grafo (no hace falta a esta
escala), tanda 2 de UX (vive en su propia lista) y la vista de capítulos.

Criterio de "madurez alcanzada" para v0.1: CI verde en dos plataformas,
≥ 8/10 ejecuciones reales llegando a revisión, ≥ 50 % de decisiones con
outcome, al menos un asiento por API con costo real medido, y una VM limpia
que llega a la UI siguiendo el README.
