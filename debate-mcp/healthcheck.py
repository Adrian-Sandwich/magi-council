#!/usr/bin/env python3
"""Chequeo de salud del sistema de colaboración: Postgres, relay y grafo.

Existe por dos fallas silenciosas que encontramos el 2026-08-26:

1. Postgres llevaba tiempo caído (un `postmaster.pid` stale apuntando a un PID
   reciclado por otro proceso). El tablero entero estaba muerto y nadie se
   enteró: el relay seguía "vivo", loguendo OperationalError cada 20s.
2. `memory.db` tenía 19 días. `refresh.sh` no estaba agendado en ningún lado y
   nadie lo corría. El grafo de memoria mostraba menos de la mitad de las
   sesiones reales, sin ninguna señal de que estuviera desactualizado.

O sea: los dos componentes fallaban ESTANDO "arriba". Por eso acá no se
chequea que los procesos existan, se chequea que estén produciendo algo
reciente.

Uso:
    python healthcheck.py            # imprime el reporte
    python healthcheck.py --notify   # además avisa por notificación de macOS
    python healthcheck.py --quiet    # sólo imprime si algo está mal

Exit code: 0 todo bien, 1 warning, 2 crítico. Sirve para agendarlo.
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

from config import CONNINFO  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
HEARTBEAT_PATH = BASE_DIR / "logs" / "relay_heartbeat.json"
MEMORY_DB = REPO_ROOT / "memory-graph" / "memory.db"

OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}

# El relay refresca el heartbeat en cada wake ocioso (300s). Dos ciclos
# perdidos ya es señal de que algo pasa.
HEARTBEAT_WARN_SECS = 700
HEARTBEAT_CRIT_SECS = 3600

# refresh.sh corre cada hora; un día entero sin re-ingesta es un grafo mintiendo.
GRAPH_WARN_SECS = 6 * 3600
GRAPH_CRIT_SECS = 48 * 3600

# Una decisión abierta durante horas es el mismo síntoma que el grafo
# envejecido: el sistema está "vivo" pero trabado. Generosos con el arranque
# (tres cabezas tardan lo suyo); un día entero abierto es un loop roto.
DECISION_WARN_SECS = 2 * 3600
DECISION_CRIT_SECS = 24 * 3600


def _age_secs(ts: float) -> float:
    return time.time() - ts


def _human(secs: float) -> str:
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}min"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def check_postgres() -> tuple[str, str]:
    try:
        with psycopg.connect(CONNINFO, connect_timeout=5) as conn:
            n_msgs = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            row = conn.execute(
                "SELECT max(version) FROM schema_version"
            ).fetchone()
    except psycopg.OperationalError as exc:
        first_line = str(exc).strip().splitlines()[0]
        return CRIT, f"postgres inalcanzable: {first_line}"
    except psycopg.Error as exc:
        return CRIT, f"postgres responde pero el esquema está mal: {exc}"

    version = row[0] if row and row[0] is not None else None
    if version is None:
        return WARN, f"{n_msgs} mensajes, pero sin schema_version (¿falta correr schema/migrate.py?)"
    return OK, f"{n_msgs} mensajes, esquema v{version}"


def check_relay() -> tuple[str, str]:
    if not HEARTBEAT_PATH.exists():
        return CRIT, "sin heartbeat: el relay nunca arrancó (o corre una versión vieja)"
    try:
        hb = json.loads(HEARTBEAT_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return CRIT, f"heartbeat ilegible: {exc}"

    ts = datetime.fromisoformat(hb["ts"])
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    detail = f"latido hace {_human(age)}, pid={hb.get('pid')}, last_id={hb.get('last_id')}"

    if age > HEARTBEAT_CRIT_SECS:
        return CRIT, f"relay congelado: {detail}"
    if not hb.get("pg_ok", True):
        return CRIT, f"relay vivo pero sin base: {detail}"
    if age > HEARTBEAT_WARN_SECS:
        return WARN, f"relay lento o trabado: {detail}"

    capped = hb.get("capped_threads") or []
    if capped:
        return WARN, f"{detail}; threads frenados por tope de rondas: {', '.join(capped)}"
    inflight = hb.get("inflight") or []
    if inflight:
        detail += f", agentes corriendo en: {', '.join(inflight)}"
    return OK, detail


# Lo que refresh.sh (horario) tiene que haber corrido. ingest_debate no está:
# lo cubre la sincronización continua del relay (memory_sync).
REFRESH_SOURCES = ("ingest_claude", "ingest_kimi", "ingest_docs", "ingest_code")


def _ingest_runs() -> dict[str, float]:
    """{ingestor: epoch de su última corrida} leído de memory.db. Vacío si la
    base es anterior a la tabla ingest_runs."""
    try:
        with sqlite3.connect(f"{MEMORY_DB.resolve().as_uri()}?mode=ro", uri=True, timeout=2) as conn:
            return dict(conn.execute("SELECT source, finished_at FROM ingest_runs").fetchall())
    except sqlite3.Error:
        return {}


def _check_sync() -> tuple[str, str]:
    """Conversaciones y decisiones: las sincroniza el relay cada 30s."""
    try:
        heartbeat = json.loads(HEARTBEAT_PATH.read_text())
        sync = heartbeat.get('memory_sync', {})
    except (OSError, ValueError):
        sync = {}
    if not sync:
        return WARN, "sin sincronización de conversaciones (relay sin heartbeat)"
    if sync.get('status') == 'error':
        return WARN, f"sincronización de conversaciones fallando ({sync.get('error', 'error')}); se reintentará"
    if sync.get('semantic_status') not in (None, 'ok'):
        return WARN, f"conversaciones sincronizadas; índice semántico pendiente ({sync['semantic_status']})"
    last_success = sync.get('last_success')
    if last_success and _age_secs(last_success) < 180:
        return OK, f"conversaciones sincronizadas hace {_human(_age_secs(last_success))}"
    return WARN, 'sincronización de conversaciones pendiente o atrasada'


def _check_refresh(runs: dict[str, float]) -> tuple[str, str]:
    """Sesiones, docs y código: los re-ingesta refresh.sh cada hora. Se mira
    la edad de CADA fuente, no el mtime de memory.db: el relay toca el archivo
    cada 30s y el mtime decía "fresco" con sesiones de días sin ingestar."""
    stale = []
    worst = OK
    for source in REFRESH_SOURCES:
        finished = runs.get(source)
        age = _age_secs(finished) if finished else None
        if age is None:
            status = WARN
            stale.append(f"{source} nunca corrió")
        elif age > GRAPH_CRIT_SECS:
            status = CRIT
            stale.append(f"{source} hace {_human(age)}")
        elif age > GRAPH_WARN_SECS:
            status = WARN
            stale.append(f"{source} hace {_human(age)}")
        else:
            continue
        if _RANK[status] > _RANK[worst]:
            worst = status
    if not stale:
        newest = max(runs[s] for s in REFRESH_SOURCES)
        return OK, f"refresh.sh corrió hace {_human(_age_secs(newest))}"
    return worst, "refresh.sh atrasado: " + ", ".join(stale)


def check_graph() -> tuple[str, str]:
    if not MEMORY_DB.exists():
        return WARN, f"{MEMORY_DB} no existe: nunca se corrió memory-graph/refresh.sh"
    runs = _ingest_runs()
    if not runs:
        # Base anterior a ingest_runs: sólo queda el mtime, que desde que el
        # relay sincroniza cada 30s ya no distingue fuentes.
        age = _age_secs(MEMORY_DB.stat().st_mtime)
        size_mb = MEMORY_DB.stat().st_size / 1e6
        detail = f"actualizado hace {_human(age)} ({size_mb:.0f} MB); sin registro por fuente, corré refresh.sh"
        if age > GRAPH_CRIT_SECS:
            return CRIT, f"grafo stale: {detail}"
        return WARN, f"grafo sin registro de corridas: {detail}"
    results = [_check_sync(), _check_refresh(runs)]
    worst = max((s for s, _ in results), key=_RANK.get)
    return worst, "; ".join(detail for _, detail in results)


def check_decisions() -> tuple[str, str]:
    """Decisiones abiertas: una que lleva horas sin cerrar suele ser una
    cabeza que no votó o un relay que no la disparó. Postgres caído no se
    reporta acá (ya lo hace check_postgres)."""
    try:
        with psycopg.connect(CONNINFO, connect_timeout=5) as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.round,
                       extract(epoch FROM now() - greatest(
                           d.created_at,
                           coalesce((SELECT max(m.created_at) FROM messages m
                                     WHERE m.thread=d.thread), d.created_at),
                           coalesce((SELECT max(p.created_at) FROM positions p
                                     WHERE p.decision_id=d.id), d.created_at)
                       )) AS age_secs
                FROM decisions d
                WHERE d.status = 'open'
                ORDER BY age_secs DESC
                """
            ).fetchall()
    except psycopg.OperationalError:
        return OK, "postgres inalcanzable (lo reporta el check de postgres)"

    if not rows:
        return OK, "sin decisiones abiertas"
    oldest_id, oldest_round, oldest_age = rows[0]
    detail = (
        f"{len(rows)} abierta(s); la más vieja hace {_human(oldest_age)} "
        f"(#{oldest_id}, ronda {oldest_round})"
    )
    if oldest_age > DECISION_CRIT_SECS:
        return CRIT, f"decisión trabada: {detail}"
    if oldest_age > DECISION_WARN_SECS:
        return WARN, f"decisión lenta en cerrar: {detail}"
    return OK, detail


CHECKS = [
    ("postgres", check_postgres),
    ("relay", check_relay),
    ("decisions", check_decisions),
    ("memory-graph", check_graph),
]

ICON = {OK: "ok  ", WARN: "WARN", CRIT: "CRIT"}


def notify(title: str, body: str) -> None:
    """Aviso al operador cuando algo se rompe. macOS: notificación nativa vía
    osascript. Windows: toast por PowerShell (BurntToast no viene de fábrica,
    así que usamos el banner de consola — el healthcheck suele correr detrás
    de una terminal o tarea; el texto queda en el log). Otros: stdout."""
    body = body.replace('"', "'")
    title = title.replace('"', "'")
    if sys.platform == "darwin":
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            capture_output=True,
        )
    elif sys.platform == "win32":
        print(f"!! {title}: {body}")
    else:
        print(f"!! {title}: {body}")


def main() -> int:
    # `do_notify` y no `notify`: la variable local pisa la función del módulo
    # y `notify(...)` explotaba con TypeError justo cuando un check fallaba.
    do_notify = "--notify" in sys.argv
    quiet = "--quiet" in sys.argv

    results = []
    worst = OK
    for name, fn in CHECKS:
        try:
            status, detail = fn()
        except Exception as exc:  # un check roto no puede tumbar el reporte
            status, detail = CRIT, f"el chequeo falló: {exc!r}"
        results.append((name, status, detail))
        if _RANK[status] > _RANK[worst]:
            worst = status

    if not (quiet and worst == OK):
        print(f"[healthcheck] {datetime.now().astimezone().isoformat(timespec='seconds')}")
        for name, status, detail in results:
            print(f"  {ICON[status]} {name:<13} {detail}")

    if do_notify and worst != OK:
        bad = [f"{n}: {d}" for n, s, d in results if s != OK]
        notify(f"ClaMi {worst.upper()}", " | ".join(bad)[:200])

    return _RANK[worst]


if __name__ == "__main__":
    raise SystemExit(main())
