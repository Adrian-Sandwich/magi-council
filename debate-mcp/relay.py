#!/usr/bin/env python3
"""Daemon relay: orquesta los turnos del tablero 'debate'.

Dos responsabilidades sobre la misma base:

1. Decisiones MAGI: escucha el canal 'decision_all' (apertura/cambio de
   ronda de una decisión) y el clásico 'debate_all' (messages). Para cada
   decisión abierta, el motor puro (decision.py) dice qué asientos faltan
   actuar en la ronda actual; el relay los dispara. Cada asiento tiene su
   candado in-flight propio (thread::seat): las tres cabezas de una decisión
   corren EN PARALELO, sin pisarse entre sí.

2. Threads libres (compatibilidad): cuando un autor postea, dispara al
   siguiente asiento del registry para que responda, hasta que el thread
   cierra con veredictos cruzados o arbitraje.

Tipos de asiento (heads.json):
- CLI: un proceso con herramientas (claude/kimi/codex/...). El prompt le
  apunta al MCP: lee el journal, investiga el artefacto y vota con
  cast_position.
- API (apihead.py): un POST a un endpoint OpenAI-compatible (Ollama, LM
  Studio, llama.cpp). No tiene herramientas: el relay le inlinea el journal
  en el prompt y el modelo responde con un tag POSITION que se registra como
  voto con la misma lógica que cast_position (board.record_position). La
  decisión no distingue de dónde salió el voto.

Lo que aprendimos de la versión anterior, en el debate 'clami-mejoras' y
viéndolo fallar en vivo el 2026-08-26:

- Hacía `time.sleep(20)` contra una base que ya empuja eventos por
  LISTEN/NOTIFY. Ahora escucha los canales de notify y despierta al instante,
  con un wake ocioso cada IDLE_WAKE_SECS que además refresca el heartbeat.
- Disparaba un proceso POR CADA fila nueva. Si dos agentes posteaban casi a
  la vez (o si el relay volvía de estar caído con backlog), salían N procesos
  concurrentes sobre el mismo thread y el debate se duplicaba en cada ronda:
  el thread 'clami-mejoras' generó 15 mensajes de más así. Ahora se colapsa a
  un disparo por turno pendiente, con candado in-flight por asiento.
- No tenía tope de rondas: sin tope, dos analistas que nunca posteen
  'veredicto' lo hacían disparar para siempre. Y como los agentes corren con
  permiso de escritura en un cwd real, eso no es sólo gasto de tokens: es
  radio de daño. Ahora hay topes por thread y por decisión.
- Hacía Popen y se olvidaba: procesos zombie, y un agente colgado quedaba
  colgado para siempre. Ahora se espera con timeout, se mata el grupo de
  procesos si se pasa, y se registra exit code y duración.
- Avanzaba el watermark antes de saber si el disparo había salido. Si el
  binario no estaba, ese turno se perdía sin reintento. Ahora lo que no
  llegó a arrancar queda en `pending` y se reintenta.
- Corría todo con cwd fijo en un solo proyecto. Ahora el cwd sale del campo
  `artifact` del thread.
- El "otro agente" era un flip binario kimi/claude hardcodeado. Ahora los
  asientos viven en heads.json: el modelo que ocupa un asiento es
  intercambiable (CLI o API) sin tocar este archivo.
"""

import json
import locale
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import apihead  # noqa: E402
import board  # noqa: E402
import memory_ctx  # noqa: E402
import memory_sync  # noqa: E402
import council_synthesis  # noqa: E402
import decision  # noqa: E402
import heads  # noqa: E402
import personas  # noqa: E402
import production  # noqa: E402
import turn_errors
from psycopg.types.json import Json  # noqa: E402

from config import connect  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "relay_state.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
HEARTBEAT_PATH = LOG_DIR / "relay_heartbeat.json"
EVENTS_PATH = LOG_DIR / "trigger_events.jsonl"

CHANNEL_ALL = "debate_all"
CHANNEL_DECISIONS = "decision_all"

# Sin notify, despertamos igual cada tanto: refresca el heartbeat y reintenta
# lo que haya quedado en `pending`.
IDLE_WAKE_SECS = 300
MAX_BACKOFF_SECS = 300

# Tope de disparos entre un 'analisis' y el siguiente (threads libres) o por
# decisión. Un debate de 3 rondas gasta ~6; 12 deja margen para idas y
# vueltas sin dejar que un loop corra indefinidamente.
MAX_TRIGGERS_PER_THREAD = 12
MAX_TRIGGERS_PER_DECISION = 12

# Un agente que no terminó en 15 minutos está colgado.
AGENT_TIMEOUT_SECS = 900

# Techo global de turnos concurrentes, sumando CLI y API.
MAX_CONCURRENT_TRIGGERS = 4

# Un disparo que no llega a arrancar (binario ausente o no ejecutable) se
# reintenta con backoff exponencial por token; tras MAX_SPAWN_ATTEMPTS fallos
# consecutivos se estaciona: avisa en el journal y no reintenta más hasta que
# llegue un mensaje nuevo al thread (antes reintentaba cada 2s para siempre).
MAX_SPAWN_ATTEMPTS = 5
SPAWN_BACKOFF_BASE_SECS = 2
SPAWN_BACKOFF_MAX_SECS = 60

# Logs de disparo: diagnóstico reciente, no auditoría (esa vive en el
# journal). Se guardan los últimos N por prefijo (thread_asiento_ / execute_).
MAX_LOGS_PER_TRIGGER = 10

# El JSONL de eventos es append-only: al pasar el cap se rota a una única
# generación .1 (telemetría, no auditoría).
EVENTS_MAX_BYTES = 10 * 1024 * 1024

# Último recurso cuando el thread no dice sobre qué proyecto opina.
DEFAULT_CWD = os.environ.get("DEBATE_DEFAULT_CWD", str(BASE_DIR.parent))

# Override manual, gana sobre el artifact.
THREAD_CWD: dict[str, str] = {}

PROTOCOL = """Roles: los asientos del registry (heads.json) como analistas, adrian como arbitro humano.
Kinds: analisis (apertura), critica, respuesta, veredicto (cierre de cada analista), arbitraje (solo adrian). Regla de 3 rounds: analisis -> criticas cruzadas -> respuestas/veredicto. Si hay desacuerdo tras el veredicto, adrian arbitra."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "relay.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("relay")

# turnos corriendo ahora mismo, identificados por token:
#   "thread"        → thread libre (un agente por thread)
#   "thread::seat"  → turno de una cabeza en una decisión (paralelo por asiento)
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_activity: dict[str, dict] = {}
_completed_turns: set[tuple[str, int]] = set()
# Keep a local guard until the durable failure is observed, including a DB outage.
_failed_turns: dict[str, int] = {}
_pending_turn_errors: dict[str, tuple[dict, str]] = {}

# procesos vivos por token (Popen de disparos CLI y ejecutores). El relay
# los siega cuando la decisión de su thread ya no está abierta ni ejecutando
# (abort del operador, cierre por el tercer voto, etc.): mejor matar un
# proceso que dejarlo quemar tokens sobre una deliberación que terminó.
_procs: dict[str, subprocess.Popen] = {}
_procs_lock = threading.Lock()

_RE_LINE_SUFFIX = re.compile(r":\d+$")
# Los journals de decisiones viven en threads 'd<id>' (provisional al abrir,
# final tras el primer INSERT — ver board.start_decision). El patrón queda
# reservado: un thread libre con ese nombre sería secuestrado por el motor.
_RE_DECISION_THREAD = re.compile(r"^d\d+$")


def _token(thread: str, seat: str | None = None) -> str:
    return f"{thread}::{seat}" if seat else thread


# ---------------------------------------------------------------- estado

def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("last_id", 0)
    state.setdefault("threads", {})
    state.setdefault("pending", [])
    state.setdefault("spawn_failures", {})
    return state


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def thread_state(state: dict, thread: str) -> dict:
    ts = state["threads"].setdefault(thread, {})
    ts.setdefault("triggers", 0)
    ts.setdefault("cwd", None)
    return ts


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rotate_events_if_needed() -> None:
    """El JSONL de eventos crece para siempre: meses de relay arriba = un
    archivo gigante que nada más lee entero. Al pasar EVENTS_MAX_BYTES lo
    renombro a .1 (una sola generación) y arranco uno nuevo."""
    try:
        if EVENTS_PATH.stat().st_size <= EVENTS_MAX_BYTES:
            return
        EVENTS_PATH.replace(EVENTS_PATH.parent / f"{EVENTS_PATH.name}.1")
    except OSError:
        pass  # un problema de rotación no puede tumbar el registro del evento


def event(kind: str, **fields) -> None:
    """Una línea JSON por evento. Es lo que hace medible al relay: cuántos
    disparos, cuánto tardan, cuántos fallan. `healthcheck.py` lee esto."""
    _rotate_events_if_needed()
    rec = {"ts": now_iso(), "event": kind, **fields}
    token = fields.get("token")
    if token:
        with _inflight_lock:
            if kind == "trigger_spawned":
                _activity[token] = {
                    "token": token, "started_at": rec["ts"],
                    "thread": fields.get("thread"), "seat": fields.get("author"),
                    "turn": fields.get("turn"), "pid": fields.get("pid"),
                }
            elif kind == "trigger_pid" and token in _activity:
                _activity[token]["pid"] = fields.get("pid")
            elif kind == "trigger_done":
                _activity.pop(token, None)
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def write_heartbeat(state: dict, pg_ok: bool) -> None:
    """Latido en disco. launchd ya reinicia el proceso si muere, pero no sabe
    distinguir 'vivo' de 'vivo y roto' — que fue exactamente el estado del
    relay mientras Postgres estuvo caído: proceso arriba, loop escupiendo
    OperationalError cada 20s, nadie enterado."""
    with _inflight_lock:
        inflight = sorted(_inflight)
        activity = [dict(_activity[t]) for t in inflight if t in _activity]
    HEARTBEAT_PATH.write_text(json.dumps({
        "ts": now_iso(),
        "pid": os.getpid(),
        "pg_ok": pg_ok,
        "memory_sync": memory_sync.sync.snapshot(),
        "last_id": state["last_id"],
        "inflight": inflight,
        "activity": activity,
        "pending": len(state["pending"]),
        "capped_threads": sorted(
            t for t, ts in state["threads"].items()
            if ts.get("triggers", 0) >= MAX_TRIGGERS_PER_THREAD
        ),
    }, indent=1))


# ---------------------------------------------------------------- cwd

def cwd_from_artifact(artifact: str | None) -> str | None:
    """El campo `artifact` dice sobre qué opina el thread ("/Users/x/repo" o
    "src/paper.rs:120"). Si es una ruta absoluta, buscamos hacia arriba la
    raíz de repo — ahí es donde tiene sentido correr al agente."""
    if not artifact:
        return None
    p = Path(_RE_LINE_SUFFIX.sub("", artifact.strip()))
    if not p.is_absolute():
        return None
    candidate = p if p.is_dir() else p.parent
    for parent in [candidate, *candidate.parents]:
        if (parent / ".git").exists():
            return str(parent)
    return str(candidate) if candidate.is_dir() else None


def resolve_cwd(conn, thread: str, ts: dict) -> str:
    if thread in THREAD_CWD:
        return THREAD_CWD[thread]
    if ts.get("cwd"):
        return ts["cwd"]
    rows = conn.execute(
        """
        SELECT artifact FROM messages
        WHERE thread = %s AND artifact IS NOT NULL
        ORDER BY id LIMIT 5
        """,
        (thread,),
    ).fetchall()
    for r in rows:
        resolved = cwd_from_artifact(r["artifact"])
        if resolved:
            ts["cwd"] = resolved
            log.info("thread %s -> cwd %s (desde artifact)", thread, resolved)
            return resolved
    ts["cwd"] = DEFAULT_CWD
    log.warning("thread %s sin artifact usable, cwd por defecto %s", thread, DEFAULT_CWD)
    return DEFAULT_CWD


# ---------------------------------------------------------------- threads libres

def thread_closed(conn, thread: str) -> bool:
    rows = conn.execute(
        """
        SELECT author, kind FROM messages
        WHERE thread = %s ORDER BY id DESC LIMIT 2
        """,
        (thread,),
    ).fetchall()
    if not rows:
        return False
    if rows[0]["kind"] == "arbitraje":
        return True
    if len(rows) == 2:
        a, b = rows
        if (
            a["kind"] == "veredicto"
            and b["kind"] == "veredicto"
            and a["author"] != b["author"]
        ):
            return True
    return False


def build_prompt(thread: str, since_id: int, last_author: str, seat_to_call: str) -> str:
    return (
        f"Continua el debate en el tablero MCP 'debate', thread '{thread}'. "
        f"Protocolo:\n{PROTOCOL}\n\n"
        f"Usa read_thread(thread='{thread}', since_id={since_id}) para ver los "
        f"mensajes nuevos de '{last_author}'. Respondé como '{seat_to_call}' "
        f"con post_message(thread='{thread}', author='{seat_to_call}', kind=..., body=...) "
        f"usando el kind que corresponda según el protocolo (critica, "
        f"respuesta o veredicto). Posteá UN SOLO mensaje. No uses "
        f"wait_messages, no hace falta: este disparo es automático. Al "
        f"terminar decime solo el id del mensaje que posteaste."
    )


def next_seat(author: str, seats: list[str]) -> str | None:
    """Quien responde en un thread libre: el siguiente asiento del registry
    (round-robin). Con dos asientos es el flip clásico del debate libre."""
    if not seats:
        return None
    if author not in seats:
        return seats[0]
    return seats[(seats.index(author) + 1) % len(seats)]


# ---------------------------------------------------------------- disparo CLI

def _agent_cmd(seat_name: str, prompt: str) -> list[str]:
    """El comando del asiento sale del registry: el modelo que lo ocupa es
    intercambiable sin tocar este archivo. ValueError si no tiene binario."""
    seat = heads.seat_by_name(seat_name)
    if seat is None or not seat.get("bin"):
        raise ValueError(f"asiento {seat_name!r} sin binario en el registry")
    return [seat["bin"], *seat.get("args", []), prompt]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Mata al agente colgado y a los procesos que haya lanzado.

    POSIX: start_new_session hizo que el hijo sea líder de su grupo, y killpg
    se lleva el árbol entero. Windows: no hay grupos POSIX ni SIGKILL;
    taskkill /T /F recorre el árbol de hijos solo. Sin esto, un agente colgado
    en Windows crasheaba el hilo supervisor (os.killpg no existe acá).
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _supervise(proc: subprocess.Popen, meta: dict) -> None:
    """Espera al agente, lo mata si se cuelga, y deja el resultado medido."""
    start = time.monotonic()
    timed_out = False
    try:
        rc = proc.wait(timeout=AGENT_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        rc = proc.wait()
    duration = round(time.monotonic() - start, 1)

    if timed_out:
        log.error("agente %s en %s colgado tras %ss, matado", meta["author"], meta["thread"], AGENT_TIMEOUT_SECS)
    elif rc != 0:
        log.error("agente %s en %s salió con rc=%s (%.0fs)", meta["author"], meta["thread"], rc, duration)
    else:
        log.info("proceso de %s en %s finalizó rc=0 (%.0fs); verificando resultado", meta["author"], meta["thread"], duration)

    try:
        error = (f'Tiempo agotado tras {AGENT_TIMEOUT_SECS}s.' if timed_out else
                 f'El proceso terminó con código {rc}.' if rc else
                 'El proceso terminó sin registrar un voto. Revisa la conexión al tablero (MCP).')
        failed = _report_turn_error(meta, error) if meta.get('decision_id') else False
        if failed:
            log.error('Turno de %s en %s detenido: %s', meta['author'], meta['thread'], error)
        event("trigger_done", rc=rc, timed_out=timed_out, duration_s=duration,
              outcome='failed' if failed else 'completed', **meta)
    finally:
        with _procs_lock:
            _procs.pop(meta["token"], None)
        with _inflight_lock:
            _inflight.discard(meta["token"])


def _report_turn_error(meta: dict, reason: str) -> bool:
    token = meta['token']
    with _inflight_lock:
        _failed_turns[token] = meta['round']
        _pending_turn_errors[token] = (meta, reason)
    try:
        with connect() as conn:
            with conn.transaction():
                failed = turn_errors.record(conn, meta['decision_id'], meta['author'], meta['round'], reason)
    except Exception:
        log.exception('No pude guardar el error de %s; el turno queda bloqueado hasta guardar el diagnóstico', token)
        return True
    with _inflight_lock:
        _pending_turn_errors.pop(token, None)
        if not failed:
            _failed_turns.pop(token, None)
    return failed


def _log_stamp() -> str:
    """Marca para nombres de log de disparo con resolución de milisegundos:
    a resolución de 1s, dos disparos del mismo asiento en el mismo segundo
    pisaban el archivo."""
    return datetime.now().strftime("%Y%m%dT%H%M%S_%f")[:-3]


def _prune_trigger_logs(prefix: str, keep: int = MAX_LOGS_PER_TRIGGER) -> None:
    """Los logs por disparo se acumulan sin tope (un relay meses arriba =
    miles de archivos). Guardamos los últimos `keep` del prefijo: son
    diagnóstico reciente, no auditoría (esa vive en el journal)."""
    try:
        logs = sorted(LOG_DIR.glob(f"{prefix}*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in logs[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def trigger(seat_name: str, thread: str, since_id: int, cwd: str, prompt: str, meta: dict | None = None) -> bool:
    """Lanza al asiento CLI. Devuelve False si no llegó a arrancar — el
    llamador lo reencola en `pending` (con backoff) en vez de perder el
    turno."""
    ts_label = _log_stamp()
    out_path = LOG_DIR / f"{thread}_{seat_name}_{ts_label}.log"
    meta = {
        "thread": thread, "author": seat_name, "since_id": since_id, "cwd": cwd,
        "token": _token(thread, seat_name) if (meta or {}).get("decision_id") else _token(thread),
        **(meta or {}),
    }

    try:
        f = out_path.open("w")
        _prune_trigger_logs(f"{thread}_{seat_name}_")
        # start_new_session es POSIX (grupo de procesos para el kill en el
        # timeout); en Windows taskkill /T ya recorre el árbol de hijos.
        popen_kw = {"start_new_session": True} if os.name == "posix" else {}
        proc = subprocess.Popen(
            _agent_cmd(seat_name, prompt),
            cwd=cwd,
            stdout=f,
            stderr=subprocess.STDOUT,
            **popen_kw,
        )
    except (OSError, ValueError) as exc:
        log.error("no pude lanzar %s en %s: %s", seat_name, thread, exc)
        event("trigger_spawn_failed", error=str(exc), **meta)
        return False

    with _inflight_lock:
        _inflight.add(meta["token"])
    with _procs_lock:
        _procs[meta["token"]] = proc
    log.info("disparo %s en thread=%s cwd=%s -> %s", seat_name, thread, cwd, out_path.name)
    event("trigger_spawned", pid=proc.pid, **meta)

    threading.Thread(target=_supervise, args=(proc, meta), daemon=True).start()
    return True


def _spawn_failure_token(thread: str, seat: str, decision_id: int | None) -> str:
    """La clave del backoff es el mismo token que usa el disparo: uno por
    thread en chat libre, uno por (thread, asiento) en decisiones."""
    return _token(thread, seat) if decision_id is not None else _token(thread)


def _registra_fallo_de_disparo(conn, state: dict, token: str, thread: str,
                               seat: str, cand: dict) -> None:
    """Un disparo que no arrancó vuelve a `pending` con backoff exponencial
    (2s, 4s, 8s... capado en SPAWN_BACKOFF_MAX_SECS). Tras MAX_SPAWN_ATTEMPTS
    fallos consecutivos del mismo token se estaciona: avisa una vez en el
    journal y no reintenta más hasta que llegue un mensaje nuevo al thread.
    Antes de esto, un binario ausente reintentaba cada 2s para siempre."""
    failures = state.setdefault("spawn_failures", {})
    info = failures.setdefault(token, {"count": 0, "retry_at": 0})
    info["count"] += 1
    if info["count"] >= MAX_SPAWN_ATTEMPTS:
        failures.pop(token, None)
        if cand.get('decision_id'):
            with conn.transaction():
                turn_errors.record(conn, cand['decision_id'], seat, cand['round'],
                                   f'No se pudo iniciar el proceso tras {MAX_SPAWN_ATTEMPTS} intentos. Revisa el ejecutable del asiento.')
            return
        log.error(
            "disparo de %s en %s estacionado: %s fallos de arranque seguidos "
            "(¿binario ausente?). No reintento más hasta que haya novedades en el thread.",
            seat, thread, MAX_SPAWN_ATTEMPTS,
        )
        event("trigger_parked", thread=thread, author=seat, attempts=MAX_SPAWN_ATTEMPTS)
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'resultado', %s, NULL)
            """,
            (thread,
             f"DISPARO ESTACIONADO — no pude lanzar a '{seat}' tras "
             f"{MAX_SPAWN_ATTEMPTS} intentos (¿el binario del asiento existe y "
             f"es ejecutable?). No voy a seguir reintentando solo: arreglá el "
             f"asiento y reactivá el thread con un mensaje nuevo."),
        )
        return
    delay = min(SPAWN_BACKOFF_BASE_SECS * 2 ** (info["count"] - 1), SPAWN_BACKOFF_MAX_SECS)
    info["retry_at"] = time.monotonic() + delay
    state["pending"].append({**cand, "retry_at": info["retry_at"]})


def _limpiar_fallos_de_disparo(state: dict, thread: str) -> None:
    """Un mensaje nuevo en el thread reactiva los reintentos: es la señal del
    operador (o del sistema) de que algo cambió."""
    failures = state.get("spawn_failures") or {}
    for token in [t for t in failures if t == thread or t.startswith(f"{thread}::")]:
        failures.pop(token, None)


# ---------------------------------------------------------------- disparo API

def _journal_inline(conn, thread: str) -> list[dict]:
    """Los últimos JOURNAL_LIMIT mensajes del thread, en orden cronológico.
    Mismo corte para turnos de decisión y de chat, de asientos API e
    inline: el prompt siempre ve la cola del journal."""
    rows = conn.execute(
        """
        SELECT author, kind, body FROM messages
        WHERE thread = %s ORDER BY id DESC LIMIT %s
        """,
        (thread, apihead.JOURNAL_LIMIT),
    ).fetchall()
    return [dict(m) for m in reversed(rows)]


def _run_decision_turn(seat_info: dict, d: dict, producir_voto, turn: str) -> None:
    """Un turno de decisión, síncrono: journal inline → voto (lo produce
    `producir_voto(journal)`) → registro con la misma lógica que
    cast_position. La conexión NO se sostiene abierta mientras el productor
    habla con el modelo o el proceso CLI (puede bloquear 10 minutos: con la
    conexión tomada, se acuartela un checkout de Postgres todo ese tiempo).
    Los fallos se guardan en el dossier y requieren reintento explícito."""
    start = time.monotonic()
    meta = {
        "thread": d["thread"], "author": seat_info["seat"], "token": _token(d["thread"], seat_info["seat"]),
        "decision_id": d["id"], "round": d["round"], "turn": turn,
    }
    try:
        with connect() as conn:
            journal = _journal_inline(conn, d["thread"])
        vote = producir_voto(journal)
        with connect() as conn:
            with conn.transaction():
                board.record_position(
                    conn, d["id"], seat_info["seat"],
                    vote["position"], vote["body"], vote["conditions"], expected_round=d['round'],
                )
        with _inflight_lock:
            _completed_turns.add((meta['token'], d['round']))
        log.info(
            "turno %s: %s votó %s en decisión %s (%.0fs)",
            turn, seat_info["seat"], vote["position"], d["id"], time.monotonic() - start,
        )
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno %s de %s en %s falló: %s", turn, seat_info["seat"], d["thread"], exc)
        reason = 'Tiempo agotado esperando la respuesta.' if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else str(exc)
        _report_turn_error(meta, reason)
        event("trigger_done", rc=1, error=reason, timed_out=isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_api_turn(seat_info: dict, d: dict, memory: str | None = None) -> None:
    """Turno de asiento API: prompt + chat + parseo del tag POSITION contra
    el endpoint OpenAI-compatible."""
    _run_decision_turn(
        seat_info, d,
        lambda journal: apihead.run_turn(seat_info, d, journal, memory=memory),
        "api",
    )


def _run_api_turn_bg(seat_info: dict, d: dict, memory: str | None = None) -> None:
    try:
        _run_api_turn(seat_info, d, memory)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(d["thread"], seat_info["seat"]))


def fire_api_turn(seat_info: dict, d: dict, memory: str | None = None) -> bool:
    """Dispara el turno de un asiento API en una decisión, en un thread propio."""
    with _inflight_lock:
        _inflight.add(_token(d["thread"], seat_info["seat"]))
    log.info(
        "turno API %s (modelo %s) en decisión %s, ronda %s",
        seat_info["seat"], seat_info.get("model"), d["id"], d["round"],
    )
    event("trigger_spawned", pid=None, thread=d["thread"], author=seat_info["seat"],
          decision_id=d["id"], round=d["round"], turn="api")
    threading.Thread(target=_run_api_turn_bg, args=(seat_info, d, memory), daemon=True).start()
    return True


# ---------------------------------------------------------------- turnos de chat libre

def _run_chat_turn(seat_info: dict, thread: str, producir_texto, turn: str) -> None:
    """Un turno de chat libre, síncrono: journal inline → respuesta (la
    produce `producir_texto(journal)`) → UN mensaje kind='respuesta'. Mismo
    contrato que el disparo CLI (un mensaje por turno). Si algo falla el
    mensaje no se inserta: el thread sigue con el mismo último autor y el
    próximo ciclo re-dispara (reintento implícito). Las cabezas no emiten
    'veredicto' en chat — sin decisión no tienen posición que votar: el
    humano cierra el chat con 'arbitraje' o el tope corta."""
    start = time.monotonic()
    meta = {"thread": thread, "author": seat_info["seat"], "token": _token(thread), "turn": turn}
    try:
        with connect() as conn:
            journal = _journal_inline(conn, thread)
        text = producir_texto(journal)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (thread, author, kind, body, artifact)
                VALUES (%s, %s, 'respuesta', %s, NULL)
                """,
                (thread, seat_info["seat"], text),
            )
        log.info("turno %s: %s respondió en %s (%.0fs)",
                 turn, seat_info["seat"], thread, time.monotonic() - start)
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno %s de %s en %s falló: %s", turn, seat_info["seat"], thread, exc)
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_api_chat_turn(seat_info: dict, thread: str) -> None:
    """Turno de chat libre de un asiento API (HTTP contra el endpoint)."""
    _run_chat_turn(seat_info, thread,
                   lambda journal: apihead.run_chat_turn(seat_info, journal),
                   "free-api")


def _run_api_chat_turn_bg(seat_info: dict, thread: str) -> None:
    try:
        _run_api_chat_turn(seat_info, thread)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(thread))


def fire_api_chat_turn(seat_info: dict, thread: str) -> bool:
    """Dispara el turno de chat de un asiento API, en un thread propio."""
    with _inflight_lock:
        _inflight.add(_token(thread))
    log.info("turno API de chat %s (modelo %s) en thread %s",
             seat_info["seat"], seat_info.get("model"), thread)
    event("trigger_spawned", pid=None, thread=thread, author=seat_info["seat"], turn="free-api")
    threading.Thread(target=_run_api_chat_turn_bg, args=(seat_info, thread), daemon=True).start()
    return True


# ------------------------------------------- cabezas CLI sin MCP (journal inline)

def _run_cli_inline(seat_info: dict, prompt: str, cwd: str, timeout: int,
                    token: str | None = None) -> str:
    """Corre una cabeza CLI con el prompt por STDIN (archivo temporal) y
    devuelve su stdout completo. STDIN en vez de argv: los prompts de turno
    tienen comillas y tildes que el re-quoting de shims .cmd (codex.cmd)
    rompería; y `codex exec -` lee el prompt de stdin de todos modos. Si
    pasa un token, el proceso queda registrado para poder abortarlo."""
    with tempfile.TemporaryDirectory(prefix=f"magi-{seat_info['seat']}-") as tmp:
        pin = Path(tmp) / "prompt.txt"
        pout = Path(tmp) / "out.txt"
        pin.write_text(prompt, encoding="utf-8")
        with pin.open("rb") as fin, pout.open("wb") as fout:
            command = [seat_info["bin"], *seat_info.get("args", [])]
            transport = seat_info.get('prompt_transport', 'stdin')
            if transport == 'file':
                command.append(f'Read the UTF-8 task file at {pin} and follow its instructions. '
                               'Return your final answer using the exact format requested in that file.')
            elif transport == 'argument':
                command.append(prompt)
            elif transport == 'stdin-only':
                pass  # Claude -p reads stdin without a positional '-' argument.
            else:
                command.append('-')
            proc = subprocess.Popen(
                command,
                cwd=cwd, stdin=fin, stdout=fout, stderr=subprocess.STDOUT,
                **({"start_new_session": True} if os.name == "posix" else {}),
            )
            if token is not None:
                with _procs_lock:
                    _procs[token] = proc
                event("trigger_pid", token=token, pid=proc.pid,
                      thread=token.split("::", 1)[0], author=seat_info["seat"])
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.wait()
                raise
            finally:
                if token is not None:
                    with _procs_lock:
                        _procs.pop(token, None)
                    prefix = re.sub(r'[^A-Za-z0-9_.-]', '_', token.replace('::', '_')) + '_'
                    log_path = LOG_DIR / f'{prefix}{_log_stamp()}.log'
                    fout.flush()
                    log_path.write_bytes(pout.read_bytes())
                    _prune_trigger_logs(prefix)
        text = _decode_cli_output(pout.read_bytes())
    if rc != 0:
        raise RuntimeError(f"{seat_info['seat']} salió rc={rc}: {text[-300:]}")
    return text


def _decode_cli_output(data: bytes) -> str:
    """Decode Windows CLIs without silently persisting replacement glyphs."""
    for encoding in dict.fromkeys(("utf-8", locale.getpreferredencoding(False), "cp1252")):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _run_cli_inline_turn(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> None:
    """Turno de decisión de una cabeza CLI que NO carga el MCP del tablero
    (p.ej. codex exec: en modo no interactivo no expone tools de servers
    externos — verificado 2026-09-12 con su propio debug log). El relay le
    inlinea el journal en el prompt (mismo builder que las cabezas API) y
    parsea el tag POSITION: de su salida; la salida completa queda como
    body del voto. El proceso puede investigar el repo con sus propias
    herramientas de lectura aunque no pueda votar por MCP."""
    def producir_voto(journal):
        if seat_info.get("tools"):
            # cabeza con herramientas propias (codex exec y sandbox): misma
            # capacidad de investigación que la cabeza MCP, mismo contrato.
            prompt = apihead.build_inline_active_prompt(seat_info["seat"], d, journal, memory, cwd)
        else:
            system, user = apihead.build_api_prompt(seat_info["seat"], d, journal, memory=memory)
            prompt = f"{system}\n\n{user}"
        text = _run_cli_inline(
            seat_info, prompt, cwd,
            seat_info.get("timeout_secs", AGENT_TIMEOUT_SECS),
            token=_token(d["thread"], seat_info["seat"]),
        )
        return apihead.parse_vote(apihead.strip_echo(text, prompt))

    _run_decision_turn(seat_info, d, producir_voto, "cli-inline")


def _run_cli_inline_turn_bg(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> None:
    try:
        _run_cli_inline_turn(seat_info, d, cwd, memory)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(d["thread"], seat_info["seat"]))


def fire_cli_inline_turn(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> bool:
    """Dispara el turno de decisión de una cabeza journal-inline."""
    with _inflight_lock:
        _inflight.add(_token(d["thread"], seat_info["seat"]))
    log.info("turno inline %s (%s) en decisión %s, ronda %s",
             seat_info["seat"], seat_info.get("name"), d["id"], d["round"])
    event("trigger_spawned", pid=None, thread=d["thread"], author=seat_info["seat"],
          decision_id=d["id"], round=d["round"], turn="cli-inline")
    threading.Thread(target=_run_cli_inline_turn_bg, args=(seat_info, d, cwd, memory), daemon=True).start()
    return True


def _run_cli_inline_chat_turn(seat_info: dict, thread: str, cwd: str) -> None:
    """Turno de chat libre de una cabeza journal-inline: prompt de charla,
    stdout completo posteado como UN mensaje 'respuesta' (igual contrato
    que el turno API de chat)."""
    def producir_texto(journal):
        system, user = apihead.build_chat_prompt(seat_info["seat"], journal)
        prompt = f"{system}\n\n{user}"
        text = _run_cli_inline(
            seat_info, prompt, cwd,
            seat_info.get("timeout_secs", AGENT_TIMEOUT_SECS),
            token=_token(thread),
        )
        return apihead.strip_echo(text, prompt).strip()

    _run_chat_turn(seat_info, thread, producir_texto, "free-inline")


def _run_cli_inline_chat_turn_bg(seat_info: dict, thread: str, cwd: str) -> None:
    try:
        _run_cli_inline_chat_turn(seat_info, thread, cwd)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(thread))


def fire_cli_inline_chat_turn(seat_info: dict, thread: str, cwd: str) -> bool:
    """Dispara el turno de chat de una cabeza journal-inline."""
    with _inflight_lock:
        _inflight.add(_token(thread))
    log.info("turno inline de chat %s (%s) en thread %s",
             seat_info["seat"], seat_info.get("name"), thread)
    event("trigger_spawned", pid=None, thread=thread, author=seat_info["seat"], turn="free-inline")
    threading.Thread(target=_run_cli_inline_chat_turn_bg, args=(seat_info, thread, cwd), daemon=True).start()
    return True


# ---------------------------------------------------------------- decisiones

def fetch_open_decisions(conn) -> list[dict]:
    """Decisiones abiertas con sus posiciones, para que el motor diga qué
    turnos faltan. Una consulta barata: lo normal es cero o una abiertas."""
    rows = conn.execute(
        """
        SELECT id, title, artifact, protocol, status, round, thread, heads, anchor_id, minority_report
        FROM decisions
        WHERE status = 'open'
        ORDER BY id
        """
    ).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    positions = conn.execute(
        """
        SELECT decision_id, head, round, position, conditions, message_id
        FROM positions
        WHERE decision_id = ANY(%s)
        ORDER BY decision_id, round, head
        """,
        (ids,),
    ).fetchall()
    by_decision: dict[int, list[dict]] = {}
    for p in positions:
        by_decision.setdefault(p["decision_id"], []).append(p)
    for r in rows:
        r["positions"] = by_decision.get(r["id"], [])
    return rows


def _persona_text(seat: str) -> str:
    if seat in personas.PERSONAS:
        return personas.system_prompt(seat)
    # asientos custom del registry sin persona dedicada: identidad genérica.
    return f"Sos el asiento '{seat}' del sistema MAGI."


def fire_decision_turns(conn, state: dict, d: dict) -> None:
    """Dispara los asientos que el motor dice que faltan en la ronda actual.

    Cada asiento tiene su candado (thread::seat): las cabezas de una misma
    decisión investigan y votan EN PARALELO."""
    ts = thread_state(state, d["thread"])
    budget_start = (d.get('minority_report') or {}).get('round_budget_start', 1)
    previous_budget = ts.get('round_budget_start')
    if previous_budget is None:
        ts['round_budget_start'] = budget_start
    elif previous_budget != budget_start:
        ts['round_budget_start'] = budget_start
        ts['triggers'] = 0
        ts.pop('capped_notified', None)
    if ts["triggers"] >= MAX_TRIGGERS_PER_DECISION:
        if not ts.get("capped_notified"):
            log.error(
                "decisión %s (%s) alcanzó el tope de %s disparos sin cerrar: corto el loop. "
                "Posteá un 'arbitraje' para cerrarla.",
                d["id"], d["thread"], MAX_TRIGGERS_PER_DECISION,
            )
            event("decision_capped", decision_id=d["id"], thread=d["thread"], triggers=ts["triggers"])
            ts["capped_notified"] = True
        for turn in decision.pending_turns(d, d['positions']):
            with _inflight_lock:
                busy = _token(d['thread'], turn['seat']) in _inflight
            if not busy:
                with conn.transaction():
                    turn_errors.record(conn, d['id'], turn['seat'], d['round'],
                                       'Se alcanzó el límite de intentos de la decisión.')
        return

    errors = turn_errors.active(d)
    turns = [turn for turn in decision.pending_turns(d, d["positions"]) if turn['seat'] not in errors]
    if not turns:
        return
    cwd = resolve_cwd(conn, d["thread"], ts)
    turn_decision = dict(d)
    try:
        rc, revision = _git(cwd, ["rev-parse", "HEAD"], timeout=10)
        if rc == 0:
            turn_decision["artifact_revision"] = revision.splitlines()[0]
    except Exception:
        pass
    # since_id fijo en el anchor: la cabeza siempre lee el journal completo
    # desde el título (sabe lo que dijeron las otras en rondas previas).
    since_id = max((d.get("anchor_id") or 1) - 1, 0)
    # Memoria del consejo: una sola consulta al grafo por tanda de turnos,
    # misma para las tres cabezas (es contexto compartido, no una ventaja).
    # Si el grafo no existe o falla, memoria queda vacío: nada cambia.
    recent = _journal_inline(conn, d['thread'])
    followup = next((m.get('body') or '' for m in reversed(recent)
                     if m.get('author') == 'adrian'), '')
    memoria = memory_ctx.memoria_para(followup + ' ' + d['title'], d.get('artifact'), thread=d['thread'])

    for turn in turns:
        if turn['seat'] in turn_errors.active(d):
            continue
        token = _token(d["thread"], turn["seat"])
        with _inflight_lock:
            busy = token in _inflight
            total = len(_inflight)
            failed = _failed_turns.get(token) == d['round']
            completed = (token, d['round']) in _completed_turns
        if completed:
            continue
        if failed:
            continue
        if busy:
            log.debug("turno de %s en %s ya está corriendo", turn["seat"], d["thread"])
            continue
        if total >= MAX_CONCURRENT_TRIGGERS:
            log.warning("techo de %s turnos concurrentes, encolo %s", MAX_CONCURRENT_TRIGGERS, turn["seat"])
            state["pending"].append({"id": state["last_id"], "thread": d["thread"], "author": turn["seat"]})
            continue
        failure = (state.get("spawn_failures") or {}).get(token)
        if failure and time.monotonic() < failure["retry_at"]:
            continue  # en backoff de un fallo de arranque: se reintenta cuando venza
        seat_info = heads.seat_by_name(turn["seat"])
        if seat_info is None:
            log.error("asiento %s ya no está en el registry, no lo puedo disparar", turn["seat"])
            event("trigger_spawn_failed", error="asiento fuera del registry", thread=d["thread"], author=turn["seat"])
            with conn.transaction():
                turn_errors.record(conn, d['id'], turn['seat'], d['round'], 'El asiento no existe en la configuración.')
            continue
        if seat_info.get("journal") == "inline":
            # cabeza CLI sin MCP (codex exec): prompt con journal inlineado
            # por stdin y voto parseado del stdout.
            if fire_cli_inline_turn(seat_info, turn_decision, cwd, memoria):
                state["spawn_failures"].pop(token, None)
                ts["triggers"] += 1
            continue
        if seat_info.get("type") == "api":
            if fire_api_turn(seat_info, turn_decision, memoria):
                state["spawn_failures"].pop(token, None)
                ts["triggers"] += 1
            continue
        if not seat_info.get("bin"):
            log.error("asiento %s no tiene binario (decisión degradada), no lo puedo disparar", turn["seat"])
            event("trigger_spawn_failed", error="asiento sin binario",
                  thread=d["thread"], author=turn["seat"], decision_id=d["id"])
            with conn.transaction():
                turn_errors.record(conn, d['id'], turn['seat'], d['round'], 'El asiento no tiene ejecutable configurado.')
            continue
        prompt = decision.build_head_prompt(turn["seat"], _persona_text(turn["seat"]), d, since_id, memory=memoria)
        meta = {"decision_id": d["id"], "round": turn["round"], "turn": turn["kind"]}
        cand = {"id": state["last_id"], "thread": d["thread"], "author": turn["seat"],
                'decision_id': d['id'], 'round': d['round']}
        if trigger(turn["seat"], d["thread"], since_id, cwd, prompt, meta):
            state["spawn_failures"].pop(token, None)
            ts["triggers"] += 1
        else:
            _registra_fallo_de_disparo(conn, state, token, d["thread"], turn["seat"], cand)


# ---------------------------------------------------------------- ciclo

def process_cycle(conn, state: dict) -> None:
    state.setdefault("spawn_failures", {})
    with _inflight_lock:
        unreported = list(_pending_turn_errors.values())
    for meta, reason in unreported:
        _report_turn_error(meta, reason)
    rows = conn.execute(
        """
        SELECT id, thread, author, kind FROM messages
        WHERE id > %s ORDER BY id
        """,
        (state["last_id"],),
    ).fetchall()

    # Un solo candidato por thread: el último mensaje. Los intermedios sólo
    # sirven para la contabilidad (resetear el cap con cada 'analisis').
    candidates: dict[str, dict] = {}
    for r in rows:
        ts = thread_state(state, r["thread"])
        if r["kind"] == "analisis":
            ts["triggers"] = 0
            ts["capped_notified"] = False
            # un 'analisis' nuevo también reactiva los disparos estacionados
            _limpiar_fallos_de_disparo(state, r["thread"])
            if r['author'] == 'adrian':
                with _inflight_lock:
                    for token in list(_failed_turns):
                        if token.startswith(r['thread'] + '::') and token not in _pending_turn_errors:
                            _failed_turns.pop(token, None)
        candidates[r["thread"]] = {"id": r["id"], "thread": r["thread"], "author": r["author"], "kind": r["kind"]}
        state["last_id"] = r["id"]

    # Lo que quedó sin disparar en ciclos anteriores, si no lo pisó algo nuevo
    # y ya venció su backoff de reintento (los disparos que fallaron vuelven
    # con retry_at; los demás se reintentan de una).
    retained = []
    for p in state["pending"]:
        retry_at = p.get("retry_at")
        if retry_at is not None and time.monotonic() < retry_at:
            retained.append(p)
            continue
        candidates.setdefault(p["thread"], p)
    state["pending"] = retained

    open_decisions = fetch_open_decisions(conn)
    open_turns = {
        (_token(d['thread'], seat), d['round'])
        for d in open_decisions for seat in d['heads']
    }
    visible_votes = {
        (_token(d['thread'], p['head']), p['round'])
        for d in open_decisions for p in d.get('positions', [])
    }
    with _inflight_lock:
        _completed_turns.intersection_update(open_turns - visible_votes)
    journal_threads = {d["thread"] for d in open_decisions}

    # --- threads libres: la lógica clásica, ahora sobre los asientos del registry
    seats = heads.seat_names()
    for thread, cand in candidates.items():
        if thread in journal_threads:
            continue  # journal de una decisión abierta: lo maneja el motor abajo
        if _RE_DECISION_THREAD.match(thread):
            # Journal de una decisión ya cerrada (o de otra corrida): sus
            # mensajes de cierre son ruido para el flip de threads libres.
            # Sin este filtro, al cerrarse una decisión los 'posicion' del
            # journal disparaban al asiento siguiente sobre el journal ya
            # cerrado — lo vio la prueba manual del 2026-09-12: un trigger
            # 'free' spawneado a los 5s de cerrada la decisión.
            continue

        ts = thread_state(state, thread)

        # adrian gobierna pero no opina: sus mensajes no disparan respuesta,
        # SALVO que abra ronda explícitamente (kind='analisis') — es la
        # señal de "quiero que las cabezas empiecen/continúen con esto",
        # la que usa el chat de la UI. next_seat resuelve al primer asiento
        # para autores fuera del registry.
        if cand["author"] == "adrian" and cand.get("kind") != "analisis":
            log.info("mensaje de adrian en %s (id=%s), no disparo", thread, cand["id"])
            continue

        if thread_closed(conn, thread):
            log.info("thread %s cerrado tras id=%s, no disparo", thread, cand["id"])
            continue

        if ts["triggers"] >= MAX_TRIGGERS_PER_THREAD:
            if not ts.get("capped_notified"):
                log.error(
                    "thread %s alcanzó el tope de %s disparos sin cerrar: "
                    "corto el loop. Posteá un 'arbitraje' o un 'analisis' nuevo para reanudar.",
                    thread, MAX_TRIGGERS_PER_THREAD,
                )
                event("thread_capped", thread=thread, triggers=ts["triggers"])
                ts["capped_notified"] = True
            continue

        with _inflight_lock:
            busy = _token(thread) in _inflight
            total = len(_inflight)
        if busy:
            log.info("thread %s ya tiene un agente corriendo, encolo id=%s", thread, cand["id"])
            state["pending"].append(cand)
            continue
        if total >= MAX_CONCURRENT_TRIGGERS:
            log.warning("techo de %s disparos concurrentes, encolo %s", MAX_CONCURRENT_TRIGGERS, thread)
            state["pending"].append(cand)
            continue

        other = next_seat(cand["author"], seats)
        if other is None:
            log.error("thread %s sin asientos en el registry, no puedo disparar", thread)
            continue
        seat_info = heads.seat_by_name(other)
        if seat_info is not None and seat_info.get("journal") == "inline":
            # chat libre de una cabeza sin MCP: el stdout se postea como
            # respuesta (igual contrato que el turno API de chat).
            cwd = resolve_cwd(conn, thread, ts)
            if fire_cli_inline_chat_turn(seat_info, thread, cwd):
                ts["triggers"] += 1
            else:
                state["pending"].append(cand)
            continue
        if seat_info is not None and seat_info.get("type") == "api":
            # el round-robin también sirve para cabezas API (Ollama y
            # compatibles): sin esto, el chat se colgaba cada vez que tocaba
            # un asiento sin binario — exigía un CLI que no existe.
            if fire_api_chat_turn(seat_info, thread):
                ts["triggers"] += 1
            else:
                state["pending"].append(cand)
            continue
        cwd = resolve_cwd(conn, thread, ts)
        # since_id-1: read_thread devuelve id > since_id, y cand["id"] es
        # justo el mensaje que disparó este trigger
        prompt = build_prompt(thread, cand["id"] - 1, cand["author"], other)
        recent = _journal_inline(conn, thread)
        question = next((m.get('body') or '' for m in reversed(recent)
                         if m.get('author') == 'adrian'), '')
        if question:
            prompt += '\n\n' + memory_ctx.memoria_para(question)
        if trigger(other, thread, cand["id"] - 1, cwd, prompt, meta={"turn": "free"}):
            state["spawn_failures"].pop(_token(thread), None)
            ts["triggers"] += 1
        else:
            _registra_fallo_de_disparo(conn, state, _token(thread), thread, other, cand)

    # --- decisiones MAGI: el motor dice qué turnos faltan; nosotros disparamos
    for d in open_decisions:
        fire_decision_turns(conn, state, d)

    # --- modo producción: ejecutores y merges. Las decisiones 'executing'
    # no reciben turnos de cabeza; esperan (o corren) al ejecutor.
    for d in fetch_executing_decisions(conn):
        token = _token(d["thread"], "executor")
        with _inflight_lock:
            busy = token in _inflight
            total = len(_inflight)
        if total >= MAX_CONCURRENT_TRIGGERS:
            break
        if busy or _ejecucion_gestionada(conn, d["thread"]):
            continue
        ts = thread_state(state, d["thread"])
        cwd = resolve_cwd(conn, d["thread"], ts)
        if cwd and fire_executor_turn(d, cwd):
            ts["triggers"] += 1
    _maybe_merge_reviews(conn)
    _reap_closed_decision_procs(conn)

    save_state(state)


def _synthesis_invoke(seat, prompt):
    prompt = apihead._persona(seat['seat']) + '\n\n' + prompt
    token = 'synthesis:' + seat['seat']
    started = time.monotonic()
    with _inflight_lock:
        _inflight.add(token)
    event('trigger_spawned', pid=None, token=token, thread='synthesis',
          author=seat['seat'], turn='synthesis')
    error = None
    timed_out = False
    try:
        if seat.get('type') == 'api':
            return apihead.chat(seat['base_url'], seat['model'], apihead._persona(seat['seat']), prompt, 120)
        with tempfile.TemporaryDirectory(prefix='magi-editor-') as cwd:
            if seat.get('journal') == 'inline':
                config = dict(seat, args=list(seat.get('args', [])))
                if 'exec' in config['args']:
                    final = Path(cwd) / 'final.txt'
                    config['args'] += ['--skip-git-repo-check', '--sandbox', 'read-only', '--output-last-message', str(final)]
                    _run_cli_inline(config, prompt, cwd, 120, token=token)
                    return final.read_text(encoding='utf-8')
                return _run_cli_inline(config, prompt, cwd, 120, token=token)
            out = Path(cwd) / 'result.txt'
            with out.open('wb') as stream:
                proc = subprocess.Popen([seat['bin'], *seat.get('args', []), prompt], cwd=cwd,
                    stdout=stream, stderr=subprocess.STDOUT,
                    **({'start_new_session': True} if os.name == 'posix' else {'creationflags': subprocess.CREATE_NO_WINDOW}))
                with _procs_lock:
                    _procs[token] = proc
                try:
                    if proc.wait(timeout=120) != 0:
                        raise RuntimeError('Synthesis provider failed')
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    proc.wait()
                    raise
            return out.read_text(encoding='utf-8', errors='replace')
    except Exception as exc:
        error = str(exc)
        timed_out = isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
        raise
    finally:
        event('trigger_done', rc=1 if error else 0, error=error,
              timed_out=timed_out,
              duration_s=round(time.monotonic() - started, 1), token=token,
              thread='synthesis', author=seat['seat'], turn='synthesis')
        with _inflight_lock:
            _inflight.discard(token)
        with _procs_lock:
            _procs.pop(token, None)


def main() -> None:
    state = load_state()
    memory_sync.sync.start()
    council_synthesis.start(_synthesis_invoke)
    log.info("relay arrancando, last_id=%s", state["last_id"])
    backoff = 1

    while True:
        try:
            with connect() as conn:
                conn.execute(f"LISTEN {CHANNEL_ALL}")
                conn.execute(f"LISTEN {CHANNEL_DECISIONS}")
                log.info("escuchando %s y %s (last_id=%s)", CHANNEL_ALL, CHANNEL_DECISIONS, state["last_id"])
                backoff = 1
                while True:
                    process_cycle(conn, state)
                    write_heartbeat(state, pg_ok=True)
                    # bloquea hasta que entre un notify o venza el wake ocioso.
                    # Con pendientes o agentes en vuelo el wake es corto: el
                    # candado in-flight se libera en otro hilo y el turno
                    # encolado se reintenta en segundos, no en IDLE_WAKE_SECS
                    # (con agentes instantáneos el notify del mensaje de A
                    # llega mientras A todavía figura corriendo: su turno
                    # queda en pending y sin esto el debate se congelaba
                    # hasta el wake ocioso). Ocioso de verdad = wake largo.
                    idle = not state["pending"] and not _inflight
                    wake = 2 if not idle else min(IDLE_WAKE_SECS, memory_sync.INTERVAL)
                    for _notify in conn.notifies(timeout=wake, stop_after=1):
                        break
        except Exception:
            log.exception("relay: ciclo caído, reintento en %ss", backoff)
            try:
                write_heartbeat(state, pg_ok=False)
            except OSError:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECS)





# ---------------------------------------------------------------- modo producción
# El ciclo deliberar → ejecutar → revisar → mergear. La deliberación es el
# flujo MAGI de siempre; esto agrega: vigilar las decisiones 'executing',
# lanzar al ejecutor (asiento CLI con flag executor en heads.json) en la
# rama magi/d<N>, abrir la revisión del diff como decisión normal, y mergear
# cuando la revisión cierra unánime.

EJECUTOR_TIMEOUT_SECS = 1800

DIFF_CHARS = 15000


def executor_seat(seats: list[dict] | None = None) -> dict | None:
    """El asiento que ejecuta planes: el marcado executor en heads.json;
    si no hay, el primer CLI con binario."""
    registry = seats if seats is not None else heads.load()
    for s in registry:
        if s.get("executor") and s.get("bin"):
            return s
    for s in registry:
        if s.get("type", "cli") == "cli" and s.get("bin"):
            return s
    return None


def _git(cwd: str, args: list[str], timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _prompt_ejecucion(d: dict, base: str, condiciones: list[str]) -> str:
    rama = decision.rama_ejecucion(d["id"])
    cond = "; ".join(condiciones) if condiciones else "ninguna explicita"
    approved_plan = (d.get("minority_report") or {}).get("approved_plan")
    plan = approved_plan or d['title']
    return (
        f"Sos el EJECUTOR del sistema MAGI. El consejo aprobó un plan y vos lo "
        f"implementás. Sos un agente autónomo: usá tus herramientas (leer, "
        f"escribir código, ejecutar tests/comandos) hasta terminar. No pidas "
        f"confirmación, no te detengas a preguntar.\n\n"
        f"Plan aprobado (decisión #{d['id']}): {plan}\n"
        f"Petición original: {d['title']}\n"
        f"Condiciones impuestas por el consejo: {cond}\n"
        f"Repositorio de trabajo: tu worktree aislado (directorio actual).\n\n"
        f"Instrucciones:\n"
        f"1. Ya estás en la rama '{rama}', en un worktree creado por MAGI "
        f"desde '{base}'. No cambies de rama ni de worktree. "
        f"Si hay cambios de un intento anterior, inspeccionalos y continuá.\n"
        f"2. Implementá el plan respetando las condiciones.\n"
        f"3. Commiteá TODO en '{rama}' con mensajes descriptivos. "
        f"NO merges, NO push, NO toques otras ramas.\n"
        f"4. Terminá con un resumen: archivos tocados y decisiones de "
        f"implementación que tomaste."
    )


def _condiciones_aprobacion(d: dict) -> list[str]:
    """Condiciones de la ronda aprobada; fallback para dossiers antiguos."""
    mr = d.get("minority_report") or {}
    if "approved_conditions" in mr:
        return list(mr["approved_conditions"])
    out = []
    for m in mr.get("minority") or []:
        for c in m.get("conditions") or []:
            if c not in out:
                out.append(c)
    return out


def _execution_failed(d: dict, detail: str) -> None:
    """Persist failure and its explanation together; retries keep the journal."""
    with connect() as conn:
        with conn.transaction():
            conn.execute(
                """UPDATE decisions SET minority_report =
                   COALESCE(minority_report, '{}'::jsonb) ||
                   '{"execution_state": "failed"}'::jsonb
                   WHERE id = %s AND status = 'executing'""", (d["id"],),
            )
            conn.execute(
                """INSERT INTO messages (thread, author, kind, body, artifact)
                   VALUES (%s, 'magi', 'consulta', %s, NULL)""",
                (d["thread"], f"EJECUCIÓN FALLIDA — {detail}. Escribí 'seguí' para reintentar o abortá la decisión."),
            )


def _execute_plan(d: dict, cwd: str) -> None:
    start = time.monotonic()
    meta = {"thread": d["thread"], "author": "executor",
            "token": _token(d["thread"], "executor"),
            "decision_id": d["id"], "turn": "execute"}
    try:
        seat = executor_seat()
        if seat is None:
            raise RuntimeError("ningun asiento puede ejecutar (sin binario en el registry)")
        if seat.get("type", "cli") == "cli" and not Path(seat["bin"]).exists():
            # Fallar ACÁ, antes de crear la rama/worktree: el binario ausente
            # no es razón para churn de git ni para reintentos calientes.
            raise RuntimeError(
                f"el binario del ejecutor ({seat['seat']}) no existe: {seat['bin']}"
            )
        run = production.plan(cwd, d["id"], (d.get("minority_report") or {}).get("execution"))
        rama, base = run["branch"], run["base_branch"]
        # Persist the immutable base before creating a worktree so retries
        # never capture a different user branch as their starting point.
        with connect() as conn:
            conn.execute(
                """
                UPDATE decisions
                SET minority_report = COALESCE(minority_report, '{}'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                (Json({"base_rama": base, "execution": run}), d["id"]),
            )
        cwd = production.prepare(run)
        prompt = _prompt_ejecucion(d, base, _condiciones_aprobacion(d))
        ts_label = _log_stamp()
        out_path = LOG_DIR / f"execute_{d['thread']}_{ts_label}.log"
        log.info("ejecutando plan de %s en %s (%s) -> %s",
                 d["thread"], cwd, seat["seat"], out_path.name)
        event("trigger_spawned", pid=None, thread=d["thread"], author=seat["seat"],
              decision_id=d["id"], round=d.get("round"), turn="execute")
        timed_out = False
        with out_path.open("w", encoding="utf-8") as f:
            _prune_trigger_logs(f"execute_{d['thread']}_")
            proc = subprocess.Popen(
                [seat["bin"], *seat.get("args", []), prompt],
                cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                **({"start_new_session": True} if os.name == "posix" else {}),
            )
            with _procs_lock:
                _procs[meta["token"]] = proc
            try:
                rc = proc.wait(timeout=seat.get("exec_timeout_secs", EJECUTOR_TIMEOUT_SECS))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                rc = proc.wait()
            finally:
                with _procs_lock:
                    _procs.pop(meta["token"], None)
        if timed_out or rc != 0:
            detalle = "colgado y matado" if timed_out else f"rc={rc}"
            _execution_failed(d, f"el ejecutor ({seat['seat']}) salió {detalle}. Log: {out_path.name}")
            event("trigger_done", rc=rc, timed_out=timed_out,
                  duration_s=round(time.monotonic() - start, 1), **meta)
            return
        # exito: diff de la rama y decision de revision con el diff en el journal
        reviewed_sha, diff = production.review_target(run)
        if not diff:
            diff = "(sin cambios: la rama no difiere de la base)"
        diff = diff[:DIFF_CHARS]
        with connect() as conn:
            with conn.transaction():
                active = conn.execute(
                    "SELECT status FROM decisions WHERE id = %s FOR UPDATE", (d["id"],)
                ).fetchone()
                if not active or active["status"] != "executing":
                    # Un abort mientras el agente salía gana sobre la
                    # publicación: el disparo igual terminó; sin este
                    # trigger_done el evento trigger_spawned queda abierto
                    # y el JSONL de métricas se tuerce.
                    event("trigger_done", rc=-1, timed_out=False,
                          error="abortado: la decisión cerró antes de publicar la revisión",
                          duration_s=round(time.monotonic() - start, 1), **meta)
                    return
                rev = board.start_decision(
                    conn,
                    title=f"Revisar implementación de #{d['id']}: {d['title']}",
                    artifact=cwd,
                    protocol="vote",
                )
                run.update(review_id=rev["decision_id"], reviewed_sha=reviewed_sha)
                conn.execute(
                    "UPDATE decisions SET minority_report = minority_report || %s::jsonb WHERE id = %s",
                    (Json({"execution": run, "execution_state": "reviewing"}), d["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO messages (thread, author, kind, body, artifact)
                    VALUES (%s, 'magi', 'analisis', %s, %s)
                    """,
                    (rev["thread"],
                     f"Revisión del commit exacto {reviewed_sha} contra {run['base_sha']}. "
                     f"Diff de la rama {rama} contra {base} (acotado a "
                     f"{DIFF_CHARS} chars). Revisá si el plan quedó bien "
                     f"implementado; los asientos CLI pueden inspeccionar el "
                     f"repo directamente.\n\n{diff}",
                     cwd),
                )
                conn.execute(
                    """
                    INSERT INTO messages (thread, author, kind, body, artifact)
                    VALUES (%s, 'magi', 'resultado', %s, NULL)
                    """,
                    (d["thread"],
                     f"Ejecución terminada ({seat['seat']}, "
                     f"{round(time.monotonic() - start)}s) — revisión "
                     f"#{rev['decision_id']} abierta con el diff."),
                )
        log.info("plan de %s ejecutado; revision %s abierta", d["thread"], rev["decision_id"])
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("ejecución de %s falló: %s", d["thread"], exc)
        _execution_failed(d, str(exc))
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_executor_turn(d: dict, cwd: str) -> None:
    """One executor/merge per Git common directory across relay processes."""
    try:
        if executor_seat() is None:
            raise RuntimeError("ningun asiento puede ejecutar (sin binario en el registry)")
        key = os.path.normcase(production.common_dir(cwd))
        with connect() as lease:
            acquired = lease.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired", (key,)
            ).fetchone()["acquired"]
            if not acquired:
                return
            try:
                # Fetch again under the repo lease: an earlier relay may have
                # finished or the operator may have aborted while we queued.
                current = lease.execute("SELECT * FROM decisions WHERE id = %s", (d["id"],)).fetchone()
                if current and current["status"] == "executing" and not _ejecucion_gestionada(lease, current["thread"]):
                    _execute_plan(current, cwd)
            finally:
                lease.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
    except Exception as exc:
        _execution_failed(d, str(exc))


def _run_executor_turn_bg(d: dict, cwd: str) -> None:
    try:
        _run_executor_turn(d, cwd)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(d["thread"], "executor"))


def fire_executor_turn(d: dict, cwd: str) -> bool:
    with _inflight_lock:
        _inflight.add(_token(d["thread"], "executor"))
    log.info("lanzando ejecutor para decisión %s (%s)", d["id"], d["thread"])
    threading.Thread(target=_run_executor_turn_bg, args=(d, cwd), daemon=True).start()
    return True


def fetch_executing_decisions(conn) -> list[dict]:
    """Decisiones production aprobadas esperando (o corriendo) ejecución."""
    return conn.execute(
        """
        SELECT id, title, artifact, protocol, status, round, thread, heads,
               anchor_id, minority_report
        FROM decisions
        WHERE status = 'executing'
        ORDER BY id
        """
    ).fetchall()


def _ejecucion_gestionada(conn, thread: str) -> bool:
    """Fallo persistido o revisión abierta; reconoce también fallos antiguos."""
    row = conn.execute(
        """
        SELECT 1 FROM decisions d
        WHERE d.thread = %s AND (
          d.minority_report->>'execution_state' IN ('failed', 'reviewing', 'merge_blocked')
          OR (d.minority_report->>'execution_state' IS NULL
            AND EXISTS (SELECT 1 FROM messages m WHERE m.thread = d.thread
              AND m.kind IN ('resultado', 'consulta')
              AND (m.body LIKE 'EJECUCIÓN FALLIDA%%' OR m.body LIKE 'Ejecución terminada%%')))
        )
        LIMIT 1
        """,
        (thread,),
    ).fetchone()
    return row is not None


def _maybe_merge_reviews(conn) -> None:
    """Only a persisted execution/review link can authorize a Git merge."""
    rows = conn.execute(
        """
        SELECT d.id, d.minority_report
        FROM decisions d JOIN decisions r
          ON r.id::text = d.minority_report->'execution'->>'review_id'
        WHERE d.status = 'executing' AND r.status = 'closed'
          AND d.minority_report->>'execution_state' = 'reviewing'
        ORDER BY d.id
        """
    ).fetchall()
    for candidate in rows:
        try:
            _merge_candidate(conn, candidate)
        except Exception:
            log.exception("no se pudo procesar la revisión de #%s", candidate["id"])


def _merge_candidate(conn, candidate: dict) -> None:
    run = candidate["minority_report"]["execution"]
    key = os.path.normcase(production.common_dir(run["repo"]))
    with conn.transaction():
        locked = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired", (key,)
        ).fetchone()["acquired"]
        if not locked:
            return
        orig = conn.execute("SELECT * FROM decisions WHERE id = %s FOR UPDATE", (candidate["id"],)).fetchone()
        if not orig or orig["status"] != "executing":
            return
        mr = orig["minority_report"] or {}
        if mr.get("execution_state") != "reviewing" or mr.get("aborted"):
            return
        run = mr["execution"]
        rev = conn.execute("SELECT * FROM decisions WHERE id = %s FOR UPDATE", (run["review_id"],)).fetchone()
        if not rev or rev["status"] != "closed":
            return
        approved = (rev["ruling"] == "yes"
                    and rev["confidence"] == decision.CONFIDENCE_UNANIMOUS
                    and not (rev.get("minority_report") or {}).get("aborted"))
        state = "merge_blocked"
        if approved:
            try:
                merged = production.merge_reviewed(run, f"MAGI: plan #{orig['id']}, revisión #{rev['id']}")
                run["merge_sha"] = merged
                state = "merged"
                body = f"MERGE OK — commit revisado {run['reviewed_sha']} integrado como {merged}."
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                body = f"MERGE DETENIDO — {exc}. Se conserva el worktree; resolvé el estado del repo o abrí un plan nuevo."
        else:
            body = f"MERGE PENDIENTE — revisión #{rev['id']} sin aprobación unánime; commit {run['reviewed_sha']} conservado para inspección."
        conn.execute(
            """UPDATE decisions SET minority_report = minority_report || %s::jsonb,
               status = CASE WHEN %s THEN 'closed' ELSE status END,
               closed_at = CASE WHEN %s THEN now() ELSE closed_at END WHERE id = %s""",
            (Json({"execution": run, "execution_state": state}), state == "merged", state == "merged", orig["id"]),
        )
        for thread in (orig["thread"], rev["thread"]):
            conn.execute(
                """INSERT INTO messages (thread, author, kind, body, artifact)
                   VALUES (%s, 'magi', 'resultado', %s, NULL)""", (thread, body),
            )



def _reap_closed_decision_procs(conn) -> None:
    """Sierra los procesos de cabezas o ejecutores cuya decisión ya no está
    abierta ni ejecutando: abort del operador, o el tercer voto que cerró la
    ronda mientras otra cabeza seguía generando (sin esto quemaba tokens
    hasta terminar sobre una deliberación que ya había terminado)."""
    with _procs_lock:
        items = list(_procs.items())
    for token, proc in items:
        thread = token.split("::")[0]
        if not _RE_DECISION_THREAD.match(thread):
            continue
        viva = conn.execute(
            """
            SELECT 1 FROM decisions
            WHERE thread = %s AND status IN ('open', 'executing')
            LIMIT 1
            """,
            (thread,),
        ).fetchone()
        if viva is not None:
            continue
        try:
            _kill_tree(proc)
        except Exception:
            pass
        with _procs_lock:
            _procs.pop(token, None)
        with _inflight_lock:
            _inflight.discard(token)
        log.info("proceso de %s siegado: la decisión ya no está abierta", token)
        event("trigger_done", rc=-1, timed_out=False,
              error="abortado: la decisión cerró", thread=thread,
              author=token.split("::")[-1], token=token)


if __name__ == "__main__":
    main()
