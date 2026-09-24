# Plan v0.2 — de herramienta terminada a herramienta usada

Estado al 2026-09-24: los cinco frentes del plan de madurez están hechos y
`v0.1` etiquetada. El consejo delibera, ejecuta planes en un worktree aislado
y los integra; hay CI, instalador, diagnóstico y manual de operación.

Y sin embargo no es real todavía, en tres sentidos que los números del propio
tablero dejan ver:

| señal (últimos 30 días) | valor |
|---|---|
| decisiones cerradas | 66 |
| con resultado reportado | 0 |
| decisiones con memoria calificada | 1 de 4 |
| turnos de cabeza fallidos | 57 |
| decisiones desde el 2026-09-19 | 1, y fue una prueba del sistema |

El lazo de calidad se construyó y nadie lo alimentó; el sistema dejó de usarse
justo cuando quedó terminado; y nada de esto vive fuera de esta laptop. El
2026-09-24 un apagón sucio dejó cuatro archivos en ceros — uno era trabajo sin
commitear y tres eran la configuración de codex, que quedó inservible hasta
que se apartó a mano. La base se salvó por suerte y no hay respaldo.

Seis frentes. Los dos primeros son de una tarde y quitan riesgo real; el
tercero es el único que no puede hacer una máquina.

## 1. Licencia y autoría (media hora)

| # | Tarea | Aceptación |
|---|---|---|
| 1.1 | `LICENSE` en la raíz (MIT) y el mismo dato en el README. | El repo público deja de ser "todos los derechos reservados": cualquiera puede clonarlo y usarlo legalmente. |

Un repositorio público sin licencia no se puede usar, ni siquiera para probar.
Es el arreglo más barato de este plan y bloquea al frente 6.

## 2. Respaldo del tablero (media jornada)

| # | Tarea | Aceptación |
|---|---|---|
| 2.1 | `backup.py`: `pg_dump` comprimido a `backups/`, con verificación de que el archivo contiene el volcado real y no cero bytes. | Un respaldo corrupto falla ruidoso en vez de dar falsa calma. |
| 2.2 | Rotación: se conservan los últimos N (14 por defecto) y se borra el resto. | Correrlo 20 días seguidos deja 14 archivos, no 20. |
| 2.3 | Agendado diario en Windows (`bin/schedule-backup.ps1`) y documentado para cron/launchd. | La tarea existe y el último respaldo tiene menos de 24 h. |
| 2.4 | `doctor.py` avisa si el último respaldo tiene más de 48 h o no existe, con el comando que lo arregla. | Chequeo `respaldo` en el diagnóstico y en el botón de la UI. |
| 2.5 | `docs/operacion.md`: cómo restaurar, probado de verdad sobre una base temporal. | La sección de restauración se ejecutó al menos una vez y dice cuánto tarda. |

Postgres es lo único irreemplazable: las decisiones, el journal, los votos y
los resultados. El grafo se reconstruye con `refresh.sh` y los worktrees son
temporales.

## 3. Dos semanas de uso real (no es código)

| # | Tarea | Aceptación |
|---|---|---|
| 3.1 | Usar el consejo para decisiones que importan, no para probar el sistema. | ≥ 10 decisiones en dos semanas sobre repos o preguntas reales. |
| 3.2 | Calificar el resultado de cada decisión cerrada («¿Cómo salió?»). | ≥ 50 % de las cerradas con resultado, que es la meta que el panel viene informando en 0 %. |
| 3.3 | Calificar la memoria que el consejo vio (👍/👎). | ≥ 30 % de las decisiones con fuentes, calificadas. |
| 3.4 | Leer el panel semanal del lunes y anotar una conclusión por semana. | Dos entradas en `docs/uso.md` con qué se decidió cambiar y por qué. |

Sin estos datos, cada ajuste de personas, memoria o modelos se decide a ojo.
Este frente no lo puede hacer una máquina y bloquea cualquier optimización
basada en evidencia.

## 4. Costo y fragilidad por turno (2–3 días)

| # | Tarea | Aceptación |
|---|---|---|
| 4.1 | Medir el costo real por decisión con la columna que ya trae `metrics.py`. | Una cifra en `docs/uso.md`: cuánto cuesta una decisión típica hoy. |
| 4.2 | Un asiento por API (clave propia) votando decisiones reales durante una semana, en paralelo a los CLI. | Comparación de latencia, costo y tasa de error contra el mismo asiento por CLI. |
| 4.3 | Decidir con esos datos qué asientos quedan por API y cuáles por CLI, y documentarlo. | Tabla en el README y `heads.json` por defecto acorde. |
| 4.4 | Reducir el contexto que el CLI carga en cada arranque, o dejar constancia de que no se puede. | El costo por turno baja, o queda escrito por qué no. |

Cada turno de claude por CLI cuesta 29 centavos a precio de lista sólo por
cargar 22 000 tokens de contexto antes de leer la pregunta, y los límites de
sesión ya tumbaron rondas dos veces. La infraestructura de API está hecha y
probada contra un servidor falso, pero nunca corrió contra un proveedor real.

## 5. Instalación verificada afuera (1 día)

| # | Tarea | Aceptación |
|---|---|---|
| 5.1 | Correr `install.ps1 -WithPostgres` en una máquina o VM limpia, de cero. | De clonar a ver la UI en menos de 10 minutos, siguiendo sólo el README. |
| 5.2 | Corregir lo que aparezca y anotar los tiempos reales. | La promesa del README es una medición, no una estimación. |
| 5.3 | Probar `install.sh` en macOS o Linux, o retirar esa promesa hasta probarla. | Lo que el README dice de cada plataforma se ejecutó en esa plataforma. |

La ruta que baja el Postgres portátil nunca se ejecutó ni una vez: la URL de
descarga es una conjetura.

## 6. Que lo use alguien más (depende de 1, 2 y 5)

| # | Tarea | Aceptación |
|---|---|---|
| 6.1 | Un demo de 30 segundos en el README (GIF o video corto): pregunta, deliberación, respuesta. | Se entiende qué hace el sistema sin instalarlo. |
| 6.2 | Una persona ajena instala y abre una decisión sin ayuda por chat. | Su instalación llega a la UI y el consejo vota. |
| 6.3 | Anotar cada punto donde esa persona se trabó. | Lista de fricciones reales, que es el siguiente plan. |

Un segundo usuario es la prueba de que el proyecto existe fuera de esta
laptop. Todo lo demás es preparación para esto.

## Orden

```
hoy       ── 1 licencia ── 2 respaldo
semana 1  ── 3 uso real (empieza y corre en paralelo con todo)
semana 2  ── 4 costo por turno ── 5 instalación en VM
semana 3  ── 6 segundo usuario
```

Qué NO está acá a propósito: multiusuario, nube, más proveedores, más UI. El
sistema ya hace lo que tiene que hacer; lo que falta es que se use, que
aguante y que se pueda entrar.
