# Evolución de la memoria MAGI

## Paso 1: recuperar contexto útil (implementado)

La ingesta de decisiones conserva el objetivo, repositorio, estado, condiciones
registradas y el último mensaje relevante de cada autor (hasta 1800 caracteres),
con su identificador y fecha. Son fuentes históricas, no hechos verificados.

La recuperación prioriza el mismo thread, después el repositorio y la coincidencia
de términos. Usa fechas del contenido y evita el antiguo relleno de decisiones
recientes sin relación. Recorre un salto del grafo hacia archivos, código y documentos.
El contexto enviado tiene un máximo de 4500 caracteres y 5 dossiers (hasta el
2026-09-18 eran 6500 y 8, y salía siempre lleno: 6.4–6.5k en 14 decisiones
seguidas) y llega al consejo y al chat. Un dossier entra sólo si comparte el
thread, coincide en el título, coincide en suficientes términos del contenido
(uno si es el mismo repositorio, dos si no) o es semánticamente cercano; del
mismo thread no se repite evidencia (el journal ya va en el prompt) y de otros
dossiers van el último mensaje humano, el resultado y una sola posición de
cabeza, 300 caracteres cada uno, con 1500 por dossier. Con eso el bloque medio
pasó de 6474 a 4116 caracteres (~1.000 tokens por cabeza y turno) sin cambiar el
recall de la evaluación con seguimientos reales (`MEMORY_SELECTIVE=0` restaura
el comportamiento anterior para comparar). En la ronda 1 de una decisión con
repositorio se antepone un mapa del repo construido desde el grafo de código
(`memory_ctx.repo_brief`): tamaño, carpetas, entradas HTTP, archivos más
referenciados y más cambiados, para que las cabezas con herramientas lean lo
relevante en vez de explorar.
El chat busca por la última intervención humana y, desde 2026-09-17, acota la
memoria al repositorio resuelto del thread y al thread mismo, igual que una
decisión. En las decisiones la consulta léxica arranca por el título: los
términos se limitan a 24 y un seguimiento largo dejaba el tema fuera.

Validación: pruebas de relevancia, identidad de repositorios, continuidad,
referencias a mensajes, recorrido de relaciones y presupuesto de contexto.

## Paso 2: actualización continua y memoria estructurada (implementado)

El relay ejecuta la ingesta de conversaciones en segundo plano al arrancar y cada
30 segundos después de terminar la anterior. Un fallo se reintenta con espera
creciente (hasta 240 segundos); cada proceso tiene un timeout de 60 segundos.
El heartbeat y `healthcheck.py` muestran el estado y la última sincronización
exitosa. Esto actualiza conversaciones y decisiones; las sesiones externas,
documentación, código y exportación 3D siguen dependiendo del refresh existente.

Sólo se reescriben nodos cuyo contenido cambió, y desde 2026-09-17 sólo se
reconsulta lo que pudo cambiar: dos consultas baratas (último `id` de mensaje por
thread; `md5` de la fila de cada decisión) deciden qué threads y decisiones
traer completos. Las marcas se confirman junto con los nodos en SQLite; un
reinicio o fallo no pierde cambios pendientes y el barrido de nodos borrados
sigue viendo el universo completo. Con el tablero actual (31 threads, ~330
mensajes) una corrida sin cambios no ejecuta ninguna agregación pesada.

Cada ingestor deja su última corrida en `ingest_runs`; `healthcheck.py` reporta
la edad de cada fuente por separado, porque el mtime de `memory.db` ya no
distingue la sincronización continua del refresh horario.

Las declaraciones humanas explícitas se guardan por conversación con fuente y
versiones. Por ejemplo:

```text
Objetivo: Publicar una versión estable
Restricción [datos]: Conservar los datos existentes
Pendiente [pruebas]: Validar la migración
```

Otra declaración del mismo tipo y clave sustituye la versión vigente, preservando
el historial. Sin clave se usa `general`; claves distintas conservan elementos
independientes. `Pendiente [pruebas]: resuelto` cierra ese pendiente; `cancelado`
también desactiva un elemento. Se ignoran declaraciones de modelos, citas y bloques
de código. El texto libre se conserva como contexto con su fuente, pero no se
convierte automáticamente en preferencias o restricciones permanentes.

## Paso 3: recuperación semántica (implementado, evaluación inicial)

La búsqueda combina coincidencias de palabras con embeddings multilingües locales
de `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, mediante
FastEmbed 0.8.0. Consulta los mismos nodos y conserva las prioridades de conversación
y repositorio, las referencias a fuentes y el recorrido acotado del grafo.
La similitud se aplica a decisiones, conversaciones y documentos. Los archivos y
símbolos de código se recuperan por palabras o relaciones; sus rutas solas producen
demasiadas asociaciones semánticas débiles. No envía textos a un servicio de
embeddings. La similitud no mide veracidad.

Instalación en Windows (desde la raíz):

```powershell
.\debate-mcp\.venv\Scripts\python.exe -m pip install -r debate-mcp/requirements-semantic.txt
.\debate-mcp\.venv\Scripts\python.exe debate-mcp/semantic_memory.py --download
```

El modelo se descarga explícitamente una vez a `memory-graph/models/` (ignorado por
Git). La consulta sólo usa archivos locales. Si falta el modelo, la dependencia
o el índice, sigue disponible la recuperación por palabras. `MEMORY_SEMANTIC=0`
desactiva la similitud en consultas.

El relay actualiza el índice después de sincronizar conversaciones. Los vectores
se guardan en SQLite por modelo y contenido; las filas obsoletas no participan
hasta reindexarse. Los lotes confirmados sobreviven a un reinicio. El estado
`semantic_status` aparece en el heartbeat y los fallos en `healthcheck.py`.

Evaluación reproducible con el modelo instalado:

```powershell
$env:CLAMI_SEMANTIC_EVAL = '1'
.\debate-mcp\.venv\Scripts\python.exe -m pytest tests/test_semantic_memory.py -s
```

En seis reformulaciones de desarrollo, la búsqueda por palabras obtuvo 0/6
primeros resultados correctos y la híbrida 5/6 con umbral de coseno 0.45. Una
consulta ajena (receta de pastel) no recuperó resultados. El umbral se ajustó con
esos mismos ejemplos: no son una evaluación independiente ni una garantía general.
El caso de autenticación sigue fallando. Ampliar el conjunto con casos reales,
negaciones, sinónimos y consultas sin respuesta antes de ajustar más el ranking.

La comparación sigue siendo exhaustiva, pero desde 2026-09-17 los vectores se
guardan como `float32` crudo (no JSON) y la similitud es una sola multiplicación
de matrices con numpy: 64 nodos pasaron de 4.1 MB a 0.77 MB y una consulta tarda
~2 ms con el modelo cargado. Las filas en JSON anteriores se reindexan solas.
Quedan pendientes un índice textual FTS y búsqueda vectorial aproximada para
historiales de miles de nodos. Los pasajes se limitan a 24 por nodo; los
documentos largos pueden perder cobertura. Las fuentes sin repositorio declarado
no pueden aislarse por proyecto con la misma garantía que las decisiones que sí
lo especifican.

**Evaluación con conversaciones reales** (`memory-graph/eval_retrieval.py`):
cada seguimiento humano del tablero (`contexto`/`arbitraje`, ≥25 caracteres,
sin reportes de resultado ni mensajes de control) es una consulta y su
decisión de origen la respuesta; el mensaje se oculta del grafo antes de
consultar (leave-one-out) y se consulta sin thread. Con 17 seguimientos reales
el 2026-09-17:

| ámbito | léxico R@1 / R@3 | híbrido R@1 / R@3 |
|---|---|---|
| sin repositorio | 0.35 / 0.53 | 0.35 / 0.59 |
| mismo repositorio | 0.53 / 0.94 | 0.65 / 0.94 |

El ámbito de repositorio pesa más que los embeddings. Lo que no se reencuentra
son seguimientos sin contenido temático («vamos con lo que falta resolver»,
«decide tú lo que sea mejor»): ninguna recuperación puede resolverlos sin el
thread, y en producción esos mensajes sí llegan con thread. Sigue sin ser un
conjunto calificado a mano: mide reencontrar el dossier de origen, no que fuera
la mejor memoria disponible.

Referencia del proveedor: https://qdrant.github.io/fastembed/examples/Supported_Models/

## Paso 4: síntesis conjunta (implementado)

El relay prepara una respuesta conjunta para el dossier cerrado, dividido o en
ejecución que recibió actividad más recientemente. No procesa automáticamente todo
el historial. Un asiento redacta y cada asiento esperado revisa la fidelidad de
la síntesis a las fuentes. Puede corregirse y revisarse una segunda vez: máximo
dos ciclos, ocho invocaciones con tres asientos, 120 segundos por invocación.
El borrador se publica antes de completar las revisiones, con la cabeza y fase
activas. Un timeout o respuesta inválida no cuenta como objeción editorial y no
provoca por sí solo otro ciclo. Si falla la corrección, se conserva el borrador
anterior como parcial. El caso #26 motivó estas pruebas de progreso y recuperación.

La interfaz presenta respuesta, puntos compartidos, diferencias y preguntas
pendientes. Un borrador que no obtuvo todas las revisiones favorables se marca
como parcial y muestra las objeciones o revisiones que no pudieron completarse.
Validar la fidelidad editorial no significa compartir las otras posturas. El
porcentaje de votos se etiqueta como acuerdo de voto, no certeza factual.
Los triángulos permanecen; los registros y aportes completos son detalle opcional.

La síntesis se guarda en el dossier, con fuentes, revisiones y versión del journal.
Si cambia el contexto o la ronda durante la redacción, no se publica el resultado
obsoleto. Un candado de Postgres impide trabajo duplicado entre relays. Reiniciar
recupera un trabajo interrumpido; un resultado parcial o fallido no se reintenta
indefinidamente sin nuevo contexto. El proceso editorial no modifica votos,
aprobaciones ni la ejecución de producción.

Límite actual: se incluyen las posiciones de la última ronda (hasta 3500 caracteres
por aporte) y tres intervenciones humanas recientes. La síntesis puede omitir
matices fuera de ese contexto. El cierre INFO ahora espera aceptación explícita
del contenido de una misma respuesta; una revisión fiel puede rechazar sus
conclusiones. Las objeciones alimentan la siguiente ronda, hasta tres rondas por
continuación humana. Desde el 2026-09-18 tres votos `info` en la primera ronda
van directo a esta evaluación (antes se forzaba una segunda ronda de contraste,
~2.5 min y ~12k tokens por pregunta también cuando las respuestas ya
coincidían; `INFO_MIN_ROUNDS` en `decision.py` la restaura). En las rondas 2+ las
posiciones de rondas anteriores llegan a cada cabeza resumidas a cabeza + cola
(900 + 400 caracteres; el voto y las condiciones sobreviven), no completas: eran
hasta 18k caracteres releídos por cabeza en cada ronda. Al agotarse el presupuesto, o faltar una revisión válida,
se entrega una respuesta provisional. Es acuerdo declarado por los modelos,
no una prueba de verdad factual ni una medida infalible de calidad.

## Paso 5: aprender de resultados (implementado)

La opción **¿Cómo salió?** permite registrar funcionó, falló, parcial o sin confirmar,
con observación, evidencia indicada y un aprendizaje opcional. Se conserva en la
misma decisión sin reabrirla ni autorizar ejecuciones. Un identificador por envío
evita duplicados al reintentar un fallo de conexión. Las correcciones son nuevos
reportes: el historial no se sobrescribe.

La migración `005_outcomes.sql` guarda los reportes y su mensaje de origen en
Postgres. La sincronización los lleva al grafo y la búsqueda textual/semántica
puede recuperar observaciones y aprendizajes. El contexto identifica el último
reporte, señala resultados anteriores diferentes y limita el aprendizaje al caso.
Los reportes del usuario no se presentan como verificación independiente.

También se conservan eventos del journal emitidos por MAGI: ejecución fallida,
merge bloqueado o completado, con referencia al mensaje; los metadatos disponibles
enlazan revisión y commits. Un merge no implica pruebas exitosas ni utilidad.
No se deducen causas de fallos automáticamente: el detalle registrado y la evidencia
siguen siendo necesarios. Esto mejora la memoria; no reentrena los modelos ni
demuestra por sí solo una mejora global en la calidad de sus respuestas.

## Paso 6: medir la mejora (base de evaluación implementada)

`tests/test_content_consensus.py` evalúa aceptación frente a mera fidelidad,
objeciones, revisiones faltantes, presupuesto renovado por continuación y el
recorrido transaccional INFO → respuesta común → corrección/cierre. Postgres usa
tablas temporales aisladas. Complementa los casos de recuperación semántica,
resultados observados y el timeout de la #26.

```powershell
$env:CLAMI_TEST_POSTGRES_DSN = 'dbname=debate host=localhost'
.\debate-mcp\.venv\Scripts\python.exe -m pytest tests/test_content_consensus.py tests/test_council_synthesis.py tests/test_memory_retrieval.py tests/test_outcomes.py -q
```

**Etiquetas humanas con el uso** (2026-09-17): cada ronda deja en el dossier qué
nodos del grafo entraron al prompt (`minority_report.memory_sources`); la tarjeta
de síntesis muestra esas fuentes con **👍 Sirvió / 👎 No sirvió**. La
calificación se guarda en `memory_feedback` (migración 006) con la foto de las
fuentes y un mensaje en el journal, se ingesta al nodo de la decisión y
`eval_retrieval.py` la resume (fracción útil, fuentes más rechazadas). Es el
conjunto calificado a mano que faltaba, recogido sin etiquetar aparte; juzga el
bloque completo, no cada fuente.

Las sesiones de las cabezas no se persisten a propósito (Casper corre con
`--no-session-persistence`, Kimi y Codex no dejan transcript por turno): lo que
concluyen ya entra al grafo por el journal (voto, evidencia, síntesis) y el
rastro de herramientas de cada turno sería ruido y disco sin valor de
recuperación. Por eso `ingest_claude` sólo ve las sesiones interactivas del
operador.

Estas evaluaciones prueban comportamiento, no que los modelos razonen mejor en
general. `debate-mcp/metrics.py` cubre la latencia: p50/p95/máximo, tasa de error
y timeouts por asiento y tipo de turno a partir de `logs/trigger_events.jsonl`,
más el tiempo de pared por ronda (del primer disparo al último cierre de la
tanda; un reintento días después cuenta como otra tanda). Con 30 días de log al
2026-09-17: melchior (kimi) `answer` p50 156 s / p95 364 s con 26 % de fallos;
balthasar (codex) y casper (claude) inline p50 32–35 s sin fallos; síntesis
10–34 s; la etapa Ollama (`api`) p95 15 min con 36–64 % de fallos. El costo en
dinero no se registra: los CLI no lo exponen. Siguen pendientes un conjunto
calificado a mano por el usuario y una comparación longitudinal de utilidad;
la evaluación con seguimientos reales del paso 3 es el sustituto disponible.
