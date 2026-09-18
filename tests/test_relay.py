"""Tests de la lógica de disparo del relay.

Escritos a partir de una falla real: el 2026-08-26, durante el debate
`clami-mejoras`, el relay generó 15 mensajes de más. Dos agentes postearon casi a
la vez, el relay disparó un proceso por CADA fila nueva en vez de uno por
thread, y cada ronda duplicó la cantidad de agentes. No había tope, así que
sólo se detuvo por casualidad, cuando dos veredictos consecutivos de autores
distintos cayeron uno detrás del otro.

Cada test de acá corresponde a una de esas causas. La sección de decisiones
MAGI cubre lo nuevo: el relay ya no calcula "el otro" a pulso; pide los turnos
pendientes al motor (decision.py) y dispara los asientos que faltan.

Los tests de threads libres usan un registry con asientos 'kimi' y 'claude'
(el mundo anterior al registry MAGI); los de decisiones usan los asientos
canónicos. Ningún test toca el registry real del repo.
"""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

import heads
import relay


@pytest.fixture(autouse=True)
def _clear_runtime_guards():
    relay._completed_turns.clear()
    relay._activity.clear()
    yield
    relay._completed_turns.clear()
    relay._activity.clear()


class FakeConn:
    """Postgres de mentira: responde las queries que usa el ciclo."""

    def __init__(self, messages, decisions=None, positions=None):
        self.messages = messages  # [{id, thread, author, kind, artifact}]
        self.decisions = decisions or []  # filas de decisions (dicts)
        self.positions = positions or []  # filas de positions (dicts)

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT id, thread, author, kind"):
            rows = [m for m in self.messages if m["id"] > params[0]]
            rows.sort(key=lambda m: m["id"])
        elif q.startswith("SELECT author, kind"):
            rows = sorted(
                (m for m in self.messages if m["thread"] == params[0]),
                key=lambda m: -m["id"],
            )[:2]
        elif q.startswith("SELECT artifact"):
            rows = [
                m for m in sorted(self.messages, key=lambda m: m["id"])
                if m["thread"] == params[0] and m.get("artifact")
            ][:5]
        elif q.startswith("SELECT id, author, kind, body"):
            rows = []
        elif q.startswith("SELECT message_id, round FROM positions"):
            rows = [p for p in self.positions if p.get("message_id") is not None]
        elif q.startswith("SELECT id, title, artifact") and "'open'" in q:
            rows = [d for d in self.decisions if d.get("status") == "open"]
        elif q.startswith("SELECT id, title, artifact"):
            # fetch_executing_decisions (modo producción): en los tests, ninguna
            rows = [d for d in self.decisions if d.get("status") == "executing"]
        elif q.startswith("SELECT d.id, d.minority_report"):
            # _maybe_merge_reviews: en los tests, ninguna revisión cerrada
            rows = []
        elif q.startswith("SELECT 1 FROM messages") or q.startswith("SELECT 1 FROM decisions d"):
            # _ejecucion_gestionada / marca de merge: en los tests, nada
            rows = []
        elif q.startswith("SELECT decision_id, head, round, position"):
            ids = params[0]
            rows = [p for p in self.positions if p["decision_id"] in ids]
        elif q.startswith("UPDATE decisions SET minority_report"):
            # fuentes de memoria de la ronda (_registrar_fuentes_de_memoria)
            # y estado de ejecución (_execute_plan)
            self.updates = getattr(self, "updates", []) + [params]
            rows = []
        elif q.startswith("SELECT status FROM decisions"):
            # publicación de la revisión: la decisión sigue ejecutando
            rows = [{"status": "executing"}]
        elif q.startswith("INSERT INTO messages"):
            self.inserted = getattr(self, "inserted", []) + [params]
            rows = []
        else:
            raise AssertionError(f"query inesperada: {q}")
        return _Result(rows)

    def transaction(self):
        return _Tx()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def msg(id, thread="t", author="kimi", kind="critica", artifact=None):
    return {"id": id, "thread": thread, "author": author, "kind": kind, "artifact": artifact}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "relay_state.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "hb.json")
    relay._inflight.clear()
    relay._activity.clear()
    relay._completed_turns.clear()
    relay._failed_turns.clear()
    relay._pending_turn_errors.clear()


def _capture_trigger(monkeypatch, calls):
    def fake_trigger(seat_name, thread, since_id, cwd, prompt, meta=None):
        calls.append({
            "author": seat_name, "thread": thread, "since_id": since_id,
            "cwd": cwd, "prompt": prompt, "meta": meta or {},
        })
        return True

    monkeypatch.setattr(relay, "trigger", fake_trigger)


def _patch_registry(monkeypatch, seats, bin=None):
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": s, "name": s, "type": "cli", "bin": bin, "args": ["-p"]}
        for s in seats
    ])


@pytest.fixture
def fired(monkeypatch, tmp_path):
    """Aísla el estado en disco y captura los disparos en vez de ejecutarlos.
    Registry con los asientos del mundo pre-MAGI, para que los tests de
    threads libres sigan ejercitando el flip kimi↔claude."""
    _isolate(monkeypatch, tmp_path)
    _patch_registry(monkeypatch, ["kimi", "claude"])
    calls = []
    _capture_trigger(monkeypatch, calls)
    return calls


@pytest.fixture
def fired_magi(monkeypatch, tmp_path):
    """Igual que `fired`, con los asientos canónicos del sistema MAGI y un
    binario simulado (el relay exige binario antes de disparar)."""
    _isolate(monkeypatch, tmp_path)
    _patch_registry(monkeypatch, ["melchior", "balthasar", "casper"], bin="/fake/bin")
    calls = []
    _capture_trigger(monkeypatch, calls)
    return calls


def fresh_state():
    return {"last_id": 0, "threads": {}, "pending": []}


def test_executor_respects_global_concurrency_limit(fired_magi, monkeypatch):
    d = {"id": 1, "thread": "d1", "status": "executing"}
    conn = FakeConn([], decisions=[d])
    calls = []
    monkeypatch.setattr(relay, "_inflight", {f"chat{i}" for i in range(relay.MAX_CONCURRENT_TRIGGERS)})
    monkeypatch.setattr(relay, "fire_executor_turn", lambda *a: calls.append(a))
    relay.process_cycle(conn, fresh_state())
    assert calls == []


def test_failed_execution_is_not_relaunched_each_cycle(fired_magi, monkeypatch):
    d = {"id": 1, "thread": "d1", "status": "executing"}
    conn = FakeConn([], decisions=[d])
    calls = []
    monkeypatch.setattr(relay, "_ejecucion_gestionada", lambda *a: True)
    monkeypatch.setattr(relay, "fire_executor_turn", lambda *a: calls.append(a))
    state = fresh_state()
    relay.process_cycle(conn, state)
    relay.process_cycle(conn, state)
    assert calls == []


# ------------------------------------------------- cierre de thread (libre)

def test_thread_abierto_dispara_al_otro_analista(fired):
    conn = FakeConn([msg(1, author="kimi", kind="critica")])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["author"] == "claude"
    assert fired[0]["since_id"] == 0   # id-1: read_thread devuelve id > since_id


def test_veredictos_cruzados_cierran_el_thread(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="veredicto"),
        msg(2, author="claude", kind="veredicto"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_dos_veredictos_del_mismo_autor_no_cierran(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="veredicto"),
        msg(2, author="kimi", kind="veredicto"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1


def test_arbitraje_cierra_el_thread(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="critica"),
        msg(2, author="adrian", kind="arbitraje"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_mensaje_de_adrian_no_dispara(fired):
    conn = FakeConn([msg(1, author="adrian", kind="critica")])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_analisis_de_adrian_abre_ronda_y_dispara_la_primera_cabeza(fired):
    """El chat de la UI: el operador escribe libre y el relay tiene que
    arrancar el round-robin (next_seat devuelve seats[0] para autores fuera
    del registry). Con el 'continue' plano por autor, el chat nunca arrancaba."""
    conn = FakeConn([msg(1, author="adrian", kind="analisis")])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["author"] == "kimi", "la primera cabeza del registry"
    assert fired[0]["since_id"] == 0


def test_arbitraje_de_adrian_no_dispara(fired):
    conn = FakeConn([msg(1, author="adrian", kind="arbitraje")])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


# ------------------------------------------------- B2: colapsar la ráfaga

def test_varios_mensajes_del_mismo_thread_disparan_una_sola_vez(fired):
    """La causa de los 15 mensajes de más: dos agentes posteando casi a la vez
    (o un backlog tras una caída) generaban un proceso por fila."""
    conn = FakeConn([
        msg(1, author="kimi", kind="critica"),
        msg(2, author="claude", kind="respuesta"),
        msg(3, author="claude", kind="respuesta"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["since_id"] == 2   # responde al último, no al primero
    assert fired[0]["author"] == "kimi"


def test_threads_distintos_disparan_por_separado(fired):
    conn = FakeConn([
        msg(1, thread="a", author="kimi"),
        msg(2, thread="b", author="claude"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["thread"] for c in fired) == ["a", "b"]


def test_thread_con_agente_corriendo_se_encola(fired):
    state = fresh_state()
    relay._inflight.add("t")
    try:
        relay.process_cycle(FakeConn([msg(1)]), state)
    finally:
        relay._inflight.discard("t")
    assert fired == []
    assert [p["id"] for p in state["pending"]] == [1]


# ------------------------------------------------- B1: tope de rondas

def test_tope_de_disparos_corta_el_loop(fired):
    state = fresh_state()
    conn = FakeConn([msg(1)])
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": "/tmp"}

    relay.process_cycle(conn, state)
    assert fired == []


def test_un_analisis_nuevo_reinicia_el_tope(fired):
    state = fresh_state()
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": "/tmp"}
    conn = FakeConn([msg(1, author="kimi", kind="analisis", artifact="/tmp")])

    relay.process_cycle(conn, state)
    assert len(fired) == 1
    assert state["threads"]["t"]["triggers"] == 1


def test_los_disparos_se_cuentan(fired):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)
    assert state["threads"]["t"]["triggers"] == 1


# ------------------------------------------------- B4: no perder turnos

def test_un_disparo_que_no_arranca_queda_pendiente(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")
    relay._inflight.clear()
    _patch_registry(monkeypatch, ["kimi", "claude"])
    monkeypatch.setattr(relay, "trigger", lambda *a, **kw: False)

    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)

    assert [p["id"] for p in state["pending"]] == [1]
    assert state["threads"]["t"]["triggers"] == 0, "un disparo fallido no gasta cupo"


def test_lo_pendiente_se_reintenta_en_el_ciclo_siguiente(fired):
    state = fresh_state()
    state["last_id"] = 1
    state["pending"] = [{"id": 1, "thread": "t", "author": "kimi"}]

    relay.process_cycle(FakeConn([msg(1)]), state)
    assert len(fired) == 1
    assert state["pending"] == []


def test_un_mensaje_nuevo_pisa_lo_pendiente_del_mismo_thread(fired):
    state = fresh_state()
    state["pending"] = [{"id": 1, "thread": "t", "author": "kimi"}]
    relay.process_cycle(FakeConn([msg(1), msg(2, author="claude")]), state)

    assert len(fired) == 1
    assert fired[0]["since_id"] == 1   # el mensaje 2, no el 1


def test_el_watermark_avanza(fired):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1), msg(2, thread="otro")]), state)
    assert state["last_id"] == 2


# ------------------------------------------------- A3: cwd por thread

def test_cwd_sale_del_artifact_del_thread(fired, tmp_path):
    repo = tmp_path / "proyecto"
    (repo / ".git").mkdir(parents=True)
    conn = FakeConn([msg(1, artifact=f"{repo}/src/x.py:42")])

    relay.process_cycle(conn, fresh_state())
    assert fired[0]["cwd"] == str(repo)


def test_cwd_cae_al_default_sin_artifact(fired):
    relay.process_cycle(FakeConn([msg(1)]), fresh_state())
    assert fired[0]["cwd"] == relay.DEFAULT_CWD


def test_cwd_from_artifact_ignora_rutas_relativas():
    assert relay.cwd_from_artifact("src/paper.rs:120") is None
    assert relay.cwd_from_artifact(None) is None
    assert relay.cwd_from_artifact("") is None


def test_cwd_from_artifact_sube_hasta_la_raiz_del_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a" / "b").mkdir(parents=True)
    archivo = repo / "a" / "b" / "x.py"
    archivo.write_text("")
    assert relay.cwd_from_artifact(str(archivo)) == str(repo)


def test_thread_cwd_manual_gana_sobre_el_artifact(fired, monkeypatch, tmp_path):
    repo = tmp_path / "proyecto"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setitem(relay.THREAD_CWD, "t", "/override")
    relay.process_cycle(FakeConn([msg(1, artifact=str(repo))]), fresh_state())
    assert fired[0]["cwd"] == "/override"


# ------------------------------------------------- decisiones MAGI

def mk_decision_row(**kw):
    base = {
        "id": 42,
        "title": "¿Hubo un ataque?",
        "artifact": None,
        "protocol": "vote",
        "status": "open",
        "round": 1,
        "thread": "d-42",
        "heads": ["melchior", "balthasar", "casper"],
        "anchor_id": 7,
    }
    base.update(kw)
    return base


def mk_pos_row(seat, position, round=1):
    return {
        "decision_id": 42, "head": seat, "round": round,
        "position": position, "conditions": None, "message_id": 1,
    }


def test_decision_abierta_dispara_a_las_cabezas_que_faltan(fired_magi):
    conn = FakeConn([], decisions=[mk_decision_row()])
    relay.process_cycle(conn, fresh_state())

    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]
    assert all(c["thread"] == "d-42" for c in fired_magi)
    assert all(c["since_id"] == 6 for c in fired_magi)  # anchor_id - 1
    assert all(c["meta"]["decision_id"] == 42 for c in fired_magi)
    assert all(c["meta"]["turn"] == "answer" for c in fired_magi)


def test_decision_no_redispara_a_la_cabeza_que_ya_voto(fired_magi):
    conn = FakeConn(
        [], decisions=[mk_decision_row()],
        positions=[mk_pos_row("melchior", "yes")],
    )
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper"]


def test_mensaje_en_journal_no_dispara_respuesta_libre(fired_magi):
    """Un 'posicion' en el journal es un turno de decisión, no un mensaje de
    debate libre: no debe saltar el flip de 'el otro asiento'."""
    conn = FakeConn(
        [msg(9, thread="d-42", author="melchior", kind="posicion")],
        decisions=[mk_decision_row()],
    )
    relay.process_cycle(conn, fresh_state())
    assert fired_magi, "sí dispara: faltan las otras dos cabezas"
    assert all(c["meta"].get("turn") in ("answer", "recast") for c in fired_magi)


def test_journal_de_decision_cerrada_no_dispara_thread_libre(fired_magi):
    """La prueba manual del 2026-09-12: al cerrarse una decisión, los mensajes
    de sus posiciones quedan en el thread d<id>. Si el relay los trata como
    debate libre, le dispara al asiento siguiente sobre el journal ya cerrado
    (en el daemon real, con cabezas reales, eso gasta tokens y ensucia el
    journal hasta el tope de disparos)."""
    conn = FakeConn(
        [msg(9, thread="d42", author="melchior", kind="posicion")],
        decisions=[],  # cerrada: no figura entre las abiertas
    )
    relay.process_cycle(conn, fresh_state())
    assert fired_magi == []


def test_decision_cerrada_no_dispara(fired_magi):
    conn = FakeConn([], decisions=[mk_decision_row(status="closed")])
    relay.process_cycle(conn, fresh_state())
    assert fired_magi == []


def test_tope_de_disparos_por_decision(fired_magi, monkeypatch):
    errors = []
    monkeypatch.setattr(relay.turn_errors, 'record', lambda *args: errors.append(args))
    state = fresh_state()
    state["threads"]["d-42"] = {"triggers": relay.MAX_TRIGGERS_PER_DECISION, "cwd": "/tmp"}
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)
    assert fired_magi == []
    assert {args[2] for args in errors} == {'melchior', 'balthasar', 'casper'}


def test_turno_de_decision_que_no_arranca_queda_pendiente(fired_magi, monkeypatch):
    monkeypatch.setattr(relay, "trigger", lambda *a, **kw: False)
    state = fresh_state()
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)
    assert {p["thread"] for p in state["pending"]} == {"d-42"}


def test_segunda_ronda_pide_recita_a_las_cabezas(fired_magi):
    conn = FakeConn(
        [], decisions=[mk_decision_row(round=2)],
        positions=[mk_pos_row(s, "yes", round=1) for s in ("melchior", "balthasar", "casper")],
    )
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]
    assert all("Revisá la tuya" in c["prompt"] for c in fired_magi)
    assert all(c["meta"]["turn"] == "recast" for c in fired_magi)


# ------------------------------------------------- estado y heartbeat

def test_el_estado_sobrevive_una_vuelta_completa(fired, tmp_path, monkeypatch):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)
    reloaded = relay.load_state()
    assert reloaded["last_id"] == 1
    assert reloaded["threads"]["t"]["triggers"] == 1


def test_load_state_completa_un_estado_viejo(monkeypatch, tmp_path):
    """El formato anterior era sólo {"last_id": N}."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"last_id": 42}))
    monkeypatch.setattr(relay, "STATE_PATH", path)

    state = relay.load_state()
    assert state == {"last_id": 42, "threads": {}, "pending": [], "spawn_failures": {}}


def test_heartbeat_reporta_los_threads_frenados(fired, tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "hb.json")
    state = fresh_state()
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": None}

    relay.write_heartbeat(state, pg_ok=True)
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["capped_threads"] == ["t"]
    assert hb["pg_ok"] is True


# ------------------------------------------------- paralelismo y asientos API

def test_tres_cabezas_de_la_misma_decision_disparan_en_paralelo(fired_magi, monkeypatch):
    """El candado in-flight es por asiento (thread::seat), no por thread:
    las tres cabezas de una decisión investigan al mismo tiempo. Con candado
    por thread, dos quedaban encoladas hasta el próximo wake (300s)."""
    def fake_trigger(seat_name, thread, since_id, cwd, prompt, meta=None):
        fired_magi.append({
            "author": seat_name, "thread": thread, "since_id": since_id,
            "cwd": cwd, "prompt": prompt, "meta": meta or {},
        })
        with relay._inflight_lock:
            relay._inflight.add(relay._token(thread, seat_name))
        return True

    monkeypatch.setattr(relay, "trigger", fake_trigger)
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]


class _SyncThread:
    """Reemplaza threading.Thread para correr el target en el hilo del test."""
    def __init__(self, target, args=(), daemon=True):
        target(*args)

    def start(self):
        pass


def test_asiento_api_dispara_turno_api_sin_proceso(fired_magi, monkeypatch):
    """Un asiento type='api' no ejecuta ningún binario: el relay dispara el
    turno API (HTTP + registro del voto vía board.record_position). Los
    asientos CLI de la misma decisión siguen yendo por trigger()."""
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "qwen3", "type": "api",
         "model": "qwen3", "base_url": "http://localhost:11434/v1"},
        {"seat": "balthasar", "name": "kimi", "type": "cli", "bin": "/fake/bin", "args": ["-p"]},
        {"seat": "casper", "name": "codex", "type": "cli", "bin": "/fake/bin", "args": ["-p"]},
    ])

    recorded = {}

    def fake_record(conn, decision_id, author, position, body, conditions=None, expected_round=None):
        recorded.update({
            "decision_id": decision_id, "author": author,
            "position": position, "body": body,
        })
        return {"action": "wait"}, 99

    monkeypatch.setattr(relay.board, "record_position", fake_record)
    monkeypatch.setattr(relay.apihead, "run_turn", lambda seat, d, journal, memory=None: {
        "position": "yes", "conditions": None, "body": "evidencia en el log",
    })
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    state = fresh_state()
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)

    assert recorded.get("author") == "melchior"
    assert recorded.get("position") == "yes"
    assert recorded.get("decision_id") == 42
    # los asientos CLI de la misma decisión sí pasaron por trigger()
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper"]
    assert state["threads"]["d-42"]["triggers"] == 3


def test_asiento_api_en_thread_libre_dispara_turno_api(fired, monkeypatch):
    """El round-robin del chat también sirve para cabezas API: sin esto el
    relay exigía un binario CLI y el chat se colgaba cada vez que tocaba un
    asiento API (casper en Ollama, p.ej.) — lo vio la demo del 2026-09-12."""
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "qwen", "type": "api",
         "model": "qwen", "base_url": "http://localhost:11434/v1"},
        {"seat": "balthasar", "name": "kimi", "type": "cli", "bin": "/fake/bin", "args": []},
    ])
    api_calls = []
    monkeypatch.setattr(relay, "fire_api_chat_turn",
                        lambda seat, thread, cwd=None: api_calls.append((seat["seat"], thread)) or True)
    relay.process_cycle(FakeConn([msg(1, author="adrian", kind="analisis")]), fresh_state())
    assert api_calls == [("melchior", "t")]
    assert fired == [], "el asiento API no pasa por el spawn CLI"


def test_turno_api_de_chat_publica_un_mensaje_respuesta(fired, monkeypatch):
    """Un turno de chat API completo: lee el journal, chatea y postea UN
    mensaje 'respuesta'. Si falla el INSERT no llega a pasar: el reintento
    sale del próximo ciclo del relay (mismo último autor)."""
    inserted = {}

    class _ChatConn(FakeConn):
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("INSERT INTO messages"):
                # VALUES (%s, %s, 'respuesta', %s, NULL): thread, author, body
                inserted.update(thread=params[0], author=params[1],
                                kind="respuesta", body=params[2])
                return _Result([])
            return super().execute(query, params)

    monkeypatch.setattr(relay, "connect", lambda: _ChatConn([]))
    monkeypatch.setattr(relay.apihead, "run_chat_turn",
                        lambda seat, journal, artifact=None, thread=None: "sí, yo lo revisaría con calma")

    relay._run_api_chat_turn(
        {"seat": "melchior", "model": "qwen", "base_url": "http://x/v1"}, "chat",
    )
    assert inserted == {
        "thread": "chat", "author": "melchior",
        "kind": "respuesta", "body": "sí, yo lo revisaría con calma",
    }


def test_kill_tree_usa_el_mecanismo_de_la_plataforma(monkeypatch):
    """Windows no tiene grupos POSIX ni SIGKILL: mata el árbol con taskkill
    /T /F. POSIX: killpg sobre el grupo del hijo. Lo que no puede pasar es
    que el hilo supervisor reviente por usar una API inexistente."""
    calls = []

    class _Proc:
        pid = 4242

    if sys.platform == "win32":
        monkeypatch.setattr(
            relay.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd),
        )
    else:
        monkeypatch.setattr(
            relay.signal, "SIGKILL", 9, raising=False,
        )
        monkeypatch.setattr(
            relay.os, "killpg",
            lambda pid, sig: calls.append((pid, sig)),
        )
    relay._kill_tree(_Proc())
    assert calls, "tuvo que intentar matar el árbol de procesos"
    if sys.platform == "win32":
        assert calls[0][:3] == ["taskkill", "/F", "/T"]
        assert str(_Proc.pid) in calls[0]


# --------------------------------------------------------- cabezas CLI journal-inline

def test_cabeza_cli_inline_parsea_el_voto_de_stdout(fired_magi, monkeypatch, tmp_path, allow_real_processes):
    """Asiento CLI sin MCP (journal='inline', p.ej. codex exec, cuyo modo
    no interactivo no expone tools de servers externos): el relay inlinea
    el journal en el prompt, corre el proceso y registra el voto parseado
    del POSITION: del stdout."""
    stub = tmp_path / "stub_vota.py"
    stub.write_text(
        "print('POSITION: yes')" + chr(10) + "print()" + chr(10) + "print('[stub] razon inline')" + chr(10),
        encoding="utf-8",
    )
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "stub-inline", "type": "cli",
         "journal": "inline", "bin": sys.executable, "args": [str(stub)]},
    ])
    recorded = {}

    def fake_record(conn, decision_id, author, position, body, conditions=None, expected_round=None):
        recorded.update(decision_id=decision_id, author=author,
                        position=position, body=body)
        return {"action": "wait"}, 1

    monkeypatch.setattr(relay.board, "record_position", fake_record)
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    relay.process_cycle(FakeConn([], decisions=[mk_decision_row(heads=['melchior'])]), fresh_state())

    assert recorded["author"] == "melchior"
    assert recorded["position"] == "yes"
    assert "razon inline" in recorded["body"]


def test_cabeza_cli_inline_en_chat_postea_su_stdout(fired, monkeypatch, tmp_path, allow_real_processes):
    """El chat libre también funciona sin MCP: el stdout completo se postea
    como un único mensaje 'respuesta'."""
    stub = tmp_path / "stub_charla.py"
    stub.write_text("print('charla de prueba del stub inline')", encoding="utf-8")
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "stub-inline", "type": "cli",
         "journal": "inline", "bin": sys.executable, "args": [str(stub)]},
    ])

    inserted = {}

    class _ChatConn(FakeConn):
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("INSERT INTO messages"):
                # VALUES (%s, %s, 'respuesta', %s, NULL): thread, author, body
                inserted.update(thread=params[0], author=params[1], body=params[2])
                return _Result([])
            return super().execute(query, params)

    monkeypatch.setattr(relay, "connect", lambda: _ChatConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    relay.process_cycle(FakeConn([msg(1, author="adrian", kind="analisis")]), fresh_state())

    assert inserted == {
        "thread": "t", "author": "melchior",
        "body": "charla de prueba del stub inline",
    }


def test_la_memoria_del_grafo_entra_al_prompt_de_la_cabeza(fired_magi, monkeypatch):
    """El relay consulta el grafo una vez por tanda de turnos y la misma
    memoria llega a todas las cabezas (es contexto compartido). Sin grafo
    (degradado) los prompts no cambian."""
    import memory_ctx
    monkeypatch.setattr(memory_ctx, "memoria_con_fuentes", lambda t, a=None, thread=None: ("MEMORIA-PRUEBA-X", ["decision:1"]))
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), fresh_state())
    assert fired_magi, "tiene que haber disparos"
    assert all("MEMORIA-PRUEBA-X" in c["prompt"] for c in fired_magi)


def test_cabeza_inline_con_tools_investiga_antes_de_votar(fired_magi, monkeypatch, tmp_path):
    """Simetria de capacidades: un asiento inline con 'tools': true (codex exec
    con sandbox) recibe un prompt que le permite investigar el repo con sus
    herramientas antes de emitir el POSITION — no el contrato pasivo de 'solo
    responde con el tag'. Las personalidades sesgan el criterio, no las
    capacidades: si solo una cabeza puede mirar el repo, el consejo entero
    queda sesgado a lo que esa cabeza ve."""
    prompts = {}

    def fake_run(seat_info, prompt, cwd, timeout, token=None):
        prompts[seat_info["seat"]] = prompt
        return "POSITION: yes\n\nvi el repo, voto si"

    monkeypatch.setattr(relay, "_run_cli_inline", fake_run)
    monkeypatch.setattr(relay.board, "record_position",
                        lambda *a, **kw: ({"action": "wait"}, 1))
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    seat_activo = {"seat": "balthasar", "name": "codex", "type": "cli",
                   "journal": "inline", "tools": True,
                   "bin": "/fake/bin", "args": ["exec"]}
    # con repositorio seleccionado: sin artefacto la instrucción es no
    # investigar el disco (el cwd sería el de MAGI, no el tema)
    d = mk_decision_row(artifact=str(tmp_path))
    cwd = str(tmp_path)
    relay._run_cli_inline_turn(seat_activo, d, cwd, memory="Memoria X")

    prompt = prompts["balthasar"]
    assert "INVESTIGÁ con tus herramientas" in prompt
    assert "SOLO LECTURA" in prompt
    assert "Memoria X" in prompt
    assert cwd in prompt
    assert "POSITION: yes|no|conditional|info" in prompt

    # sin tools: el prompt pasivo NO invita a investigar
    prompts.clear()
    seat_pasivo = dict(seat_activo)
    del seat_pasivo["tools"]
    relay._run_cli_inline_turn(seat_pasivo, d, cwd, memory=None)
    assert "INVESTIGÁ con tus herramientas" not in prompts["balthasar"]


def test_journal_inline_caps_large_agent_outputs():
    messages = [
        {"author": "a", "kind": "posicion", "body": "A" * 9_000},
        {"author": "b", "kind": "posicion", "body": "B" * 9_000},
        {"author": "c", "kind": "posicion", "body": "C" * 9_000},
        {"author": "adrian", "kind": "contexto", "body": "latest"},
    ]

    class JournalConn:
        def execute(self, query, params=()):
            return _Result(list(reversed(messages)))

    journal = relay._journal_inline(JournalConn(), "d30")

    assert journal[-1]["body"] == "latest"
    assert sum(len(m["body"]) for m in journal) <= relay.apihead.JOURNAL_CHAR_LIMIT + 100
    assert all(len(m["body"]) <= relay.apihead.JOURNAL_MESSAGE_CHAR_LIMIT + 40 for m in journal)


def test_synthesis_does_not_duplicate_configured_sandbox(monkeypatch):
    seen = {}

    def fake_run(config, prompt, cwd, timeout, token=None):
        seen["args"] = config["args"]
        output = config["args"][config["args"].index("--output-last-message") + 1]
        Path(output).write_text("synthesis ok", encoding="utf-8")
        return ""

    monkeypatch.setattr(relay, "_run_cli_inline", fake_run)
    monkeypatch.setattr(relay, "event", lambda *a, **kw: None)
    seat = {"seat": "balthasar", "type": "cli", "journal": "inline",
            "bin": "codex", "args": ["exec", "--sandbox", "workspace-write"]}

    assert relay._synthesis_invoke(seat, "compose") == "synthesis ok"
    assert seen["args"].count("--sandbox") == 1


# ------------------------------------------------- reintentos de disparo fallido

class _Clock:
    """Reloj monotónico controlado por el test: los backoff son exponenciales
    y el estacionamiento llega al tope en segundos de reloj, no de pared."""

    def __init__(self):
        self.now = 1_000_000.0

    def monotonic(self):
        return self.now

    def avanzar(self, secs):
        self.now += secs


class _InsertConn(FakeConn):
    """FakeConn que además captura los INSERT del journal (aviso de
    estacionamiento de un disparo)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.inserts = []

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO messages"):
            self.inserts.append(params)
            return _Result([])
        return super().execute(query, params)


def _spawn_que_no_arranca(monkeypatch):
    """Popen que falla como un binario ausente: OSError, el caso que antes
    reintentaba cada 2s para siempre."""
    def _boom(*a, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(relay.subprocess, "Popen", _boom)


def test_disparo_fallido_hace_backoff_y_al_final_estaciona(monkeypatch, tmp_path):
    """Un binario ausente no puede reintentar cada 2s para siempre: el
    reintento respeta el backoff y, tras MAX_SPAWN_ATTEMPTS fallos, se
    estaciona con aviso en el journal."""
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")
    relay._inflight.clear()
    clock = _Clock()
    monkeypatch.setattr(relay.time, "monotonic", clock.monotonic)
    _patch_registry(monkeypatch, ["kimi", "claude"], bin="/fake/bin")
    _spawn_que_no_arranca(monkeypatch)

    state = fresh_state()
    conn = _InsertConn([msg(1, author="kimi")])
    relay.process_cycle(conn, state)

    token = relay._token("t")
    assert state["spawn_failures"][token]["count"] == 1
    assert state["pending"][0]["retry_at"] > clock.now, "backoff antes de reintentar"
    assert state["threads"]["t"]["triggers"] == 0, "un disparo fallido no gasta cupo"

    # antes de que venza el backoff no se reintenta: pending se conserva
    relay.process_cycle(conn, state)
    assert state["spawn_failures"][token]["count"] == 1

    intentos = 1
    while state["spawn_failures"].get(token):
        clock.avanzar(state["spawn_failures"][token]["retry_at"] - clock.now + 0.1)
        relay.process_cycle(conn, state)
        if state["spawn_failures"].get(token):
            intentos += 1

    assert intentos == relay.MAX_SPAWN_ATTEMPTS - 1, "parked en el último intento"
    assert state["pending"] == [], "estacionado: nada queda pendiente"
    assert len(conn.inserts) == 1, "un solo aviso en el journal"
    thread, body = conn.inserts[0]
    assert thread == "t"
    assert "ESTACIONADO" in body and "claude" in body

    # un mensaje nuevo reactiva los reintentos (conteo desde cero)
    conn.messages.append(msg(2, author="kimi", kind="analisis"))
    relay.process_cycle(conn, state)
    assert state["spawn_failures"][token]["count"] == 1


def test_turno_de_decision_fallido_respeta_el_backoff(monkeypatch, tmp_path):
    """Los turnos de decisión se re-disparan desde fire_decision_turns en cada
    ciclo (sin pasar por pending): el backoff tiene que frenar ese re-drive."""
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")
    relay._inflight.clear()
    clock = _Clock()
    monkeypatch.setattr(relay.time, "monotonic", clock.monotonic)
    _patch_registry(monkeypatch, ["melchior", "balthasar", "casper"], bin="/fake/bin")
    _spawn_que_no_arranca(monkeypatch)

    state = fresh_state()
    conn = _InsertConn([], decisions=[mk_decision_row()])
    relay.process_cycle(conn, state)

    tokens = [relay._token("d-42", s) for s in ("melchior", "balthasar", "casper")]
    assert all(state["spawn_failures"][t]["count"] == 1 for t in tokens)

    # el ciclo siguiente no re-dispara nada: todos en backoff
    relay.process_cycle(conn, state)
    assert all(state["spawn_failures"][t]["count"] == 1 for t in tokens)

    clock.avanzar(relay.SPAWN_BACKOFF_BASE_SECS + 0.1)
    relay.process_cycle(conn, state)
    assert all(state["spawn_failures"][t]["count"] == 2 for t in tokens)


def test_ejecutor_con_binario_ausente_falla_rapido(monkeypatch, tmp_path):
    """Un ejecutor cuyo binario no existe falla ANTES de tocar git (rama,
    worktree) y con un mensaje claro, en vez de hot-loopar churn de git."""
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "ejec", "name": "x", "type": "cli", "executor": True,
         "bin": str(tmp_path / "no-existe")},
    ])
    plan_llamado = []
    monkeypatch.setattr(relay.production, "plan",
                        lambda *a, **kw: plan_llamado.append(a))
    fallos = []
    monkeypatch.setattr(relay, "_execution_failed",
                        lambda d, detail, **kw: fallos.append(detail))

    relay._execute_plan({"id": 7, "thread": "d7", "title": "plan X"}, str(tmp_path))

    assert plan_llamado == [], "no tiene que crear rama ni worktree"
    assert len(fallos) == 1 and "no existe" in fallos[0]


# ------------------------------------------------- logs y eventos acotados

def test_eventos_se_rotan_al_pasar_el_cap(monkeypatch, tmp_path):
    """El JSONL de eventos es append-only: al pasar el cap, el viejo pasa a
    .1 (una generación) y el nuevo arranca limpio."""
    events = tmp_path / "events.jsonl"
    events.write_text("x" * 500)
    monkeypatch.setattr(relay, "EVENTS_PATH", events)
    monkeypatch.setattr(relay, "EVENTS_MAX_BYTES", 100)

    relay.event("prueba", dato=1)

    assert events.exists()
    lineas = events.read_text().strip().splitlines()
    assert len(lineas) == 1 and json.loads(lineas[0])["event"] == "prueba"
    viejo = tmp_path / "events.jsonl.1"
    assert viejo.exists() and len(viejo.read_text()) == 500


def test_prune_trigger_logs_mantiene_los_ultimos_n(monkeypatch, tmp_path):
    """Un log por disparo, rotados: se conservan los últimos MAX_LOGS_PER_TRIGGER
    del prefijo; el resto se borra."""
    monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
    for i in range(relay.MAX_LOGS_PER_TRIGGER + 5):
        p = tmp_path / f"t_kimi_20260101T000000_{i:03d}.log"
        p.write_text("log")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    # otro asiento del mismo thread no se toca
    otro = tmp_path / "t_claude_20260101T000000_000.log"
    otro.write_text("log")

    relay._prune_trigger_logs("t_kimi_")

    vivos = sorted(p.name for p in tmp_path.glob("t_kimi_*.log"))
    assert len(vivos) == relay.MAX_LOGS_PER_TRIGGER
    assert vivos[-1].endswith("_014.log"), "sobreviven los más nuevos"
    assert otro.exists()


def test_nombre_de_log_con_milisegundos_no_colisiona(monkeypatch, tmp_path):
    """Dos disparos del mismo asiento en el mismo segundo tenían el mismo
    nombre de archivo (resolución de 1s) y el segundo pisaba al primero."""
    from datetime import datetime as _dt

    monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")

    class _MsClock:
        ms = 0

        @classmethod
        def now(cls, tz=None):
            cls.ms += 1
            return _dt(2026, 1, 1, 0, 0, 0, cls.ms * 1000, tzinfo=tz)

    monkeypatch.setattr(relay, "datetime", _MsClock)

    class _Proc:
        pid = 4321

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **kw: _Proc())
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    _patch_registry(monkeypatch, ["kimi", "claude"], bin="/fake/bin")

    assert relay.trigger("claude", "t", 0, str(tmp_path), "prompt")
    assert relay.trigger("claude", "t", 1, str(tmp_path), "prompt")
    assert len(list(tmp_path.glob("t_claude_*.log"))) == 2


# ------------------------------------------------- conexión durante el chat API

def test_turno_api_suelta_la_conexion_mientras_chatea(monkeypatch, tmp_path):
    """Regresión: _run_api_turn retenía la conexión de Postgres durante todo
    el chat (hasta 10 min por default). El journal se lee, la conexión se
    cierra, y el voto se registra en una conexión nueva."""
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")

    class _TrackedConn(FakeConn):
        abiertas = 0
        abiertas_durante_chat = None

        def __init__(self, *a, **kw):
            super().__init__([])

        def __enter__(self):
            type(self).abiertas += 1
            return self

        def __exit__(self, *a):
            type(self).abiertas -= 1
            return False

    def fake_run_turn(seat, d, journal, memory=None):
        _TrackedConn.abiertas_durante_chat = _TrackedConn.abiertas
        return {"position": "yes", "conditions": None, "body": "ok"}

    monkeypatch.setattr(relay.apihead, "run_turn", fake_run_turn)
    monkeypatch.setattr(relay.board, "record_position",
                        lambda *a, **kw: ({"action": "wait"}, 1))
    monkeypatch.setattr(relay, "connect", _TrackedConn)

    relay._run_api_turn(
        {"seat": "melchior", "type": "api", "model": "qwen", "base_url": "http://x/v1"},
        mk_decision_row(),
    )

    assert _TrackedConn.abiertas_durante_chat == 0, "chat sin conexión tomada"
    assert _TrackedConn.abiertas == 0


# ------------------------------------------------- memoria acotada y síntesis

def test_chat_libre_acota_la_memoria_al_repo_y_al_thread(fired, monkeypatch):
    """El chat recibía memoria de cualquier proyecto: `memoria_para` se
    llamaba sólo con la pregunta. Ahora viaja con el cwd resuelto del thread
    y el thread mismo, igual que en una decisión."""
    import memory_ctx

    class _JournalConn(FakeConn):
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("SELECT id, author, kind, body"):
                return _Result([{"id": 1, "author": "adrian", "kind": "analisis", "body": "¿Qué pasa con auth?"}])
            return super().execute(query, params)

    seen = []
    monkeypatch.setattr(memory_ctx, "memoria_para",
                        lambda q, artifact=None, thread=None: seen.append((q, artifact, thread)) or "MEM")
    monkeypatch.setattr(relay, "resolve_cwd", lambda conn, thread, ts: "/repo/x")
    relay.process_cycle(_JournalConn([msg(1, author="adrian", kind="analisis")]), fresh_state())
    assert seen == [("¿Qué pasa con auth?", "/repo/x", "t")]
    assert fired and "MEM" in fired[0]["prompt"]


def test_turno_api_de_chat_recibe_el_cwd_del_thread(fired, monkeypatch):
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "qwen", "type": "api",
         "model": "qwen", "base_url": "http://localhost:11434/v1"},
        {"seat": "balthasar", "name": "kimi", "type": "cli", "bin": "/fake/bin", "args": []},
    ])
    api_calls = []
    monkeypatch.setattr(relay, "resolve_cwd", lambda conn, thread, ts: "/repo/y")
    monkeypatch.setattr(relay, "fire_api_chat_turn",
                        lambda seat, thread, cwd=None: api_calls.append((seat["seat"], thread, cwd)) or True)
    relay.process_cycle(FakeConn([msg(1, author="adrian", kind="analisis")]), fresh_state())
    assert api_calls == [("melchior", "t", "/repo/y")]


def test_la_consulta_a_memoria_empieza_por_el_titulo(fired_magi, monkeypatch):
    """Los términos de búsqueda se acotan: si el follow-up humano es largo y
    va primero, el tema de la decisión queda fuera de la consulta léxica."""
    import memory_ctx
    seen = []
    monkeypatch.setattr(memory_ctx, "memoria_con_fuentes",
                        lambda q, a=None, thread=None: seen.append((q, a, thread)) or ("", []))
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row(artifact="/repo/z")]), fresh_state())
    assert seen and seen[0][0].startswith("¿Hubo un ataque?")
    assert seen[0][1:] == ("/repo/z", "d-42")


def test_la_sintesis_usa_un_cwd_estable_por_asiento(monkeypatch, tmp_path):
    """Un tempdir nuevo por síntesis dejaba un proyecto fantasma en
    ~/.claude/projects en cada corrida (57 en una semana). El cwd es uno por
    asiento, bajo logs/, y se reutiliza."""
    _isolate(monkeypatch, tmp_path)
    cwds = []

    def fake_run(config, prompt, cwd, timeout, token=None):
        cwds.append(cwd)
        return "ok"

    monkeypatch.setattr(relay, "_run_cli_inline", fake_run)
    monkeypatch.setattr(relay, "event", lambda *a, **kw: None)
    seat = {"seat": "casper", "type": "cli", "journal": "inline", "bin": "claude", "args": ["-p"]}
    assert relay._synthesis_invoke(seat, "compose") == "ok"
    assert relay._synthesis_invoke(seat, "compose") == "ok"
    assert cwds[0] == cwds[1] == str(tmp_path / "synthesis" / "casper")
    assert Path(cwds[0]).is_dir()


def test_is_timeout_reconoce_el_timeout_envuelto_por_urllib():
    import urllib.error
    assert relay._is_timeout(TimeoutError())
    assert relay._is_timeout(relay.subprocess.TimeoutExpired("x", 1))
    assert relay._is_timeout(urllib.error.URLError(TimeoutError("timed out")))
    assert not relay._is_timeout(urllib.error.URLError(ConnectionRefusedError()))
    assert not relay._is_timeout(RuntimeError("otra cosa"))


# ------------------------------------------------- crash de arranque del CLI

class _FakeProc:
    """Popen de mentira: escribe `output` en el stdout que le pasaron y sale
    con `rc`. Sirve para probar _run_cli_inline sin lanzar nada."""
    pid = 4242

    def __init__(self, rc, output=b""):
        self.rc, self.output = rc, output

    def wait(self, timeout=None):
        return self.rc


def _fake_popen(outcomes):
    """Cada llamada consume el siguiente (rc, salida) de la lista."""
    calls = []

    def popen(command, cwd=None, stdin=None, stdout=None, stderr=None, **kw):
        rc, output = outcomes.pop(0)
        # el relay abre el log en binario (turnos) o en texto (ejecutor)
        stdout.write(output if "b" in getattr(stdout, "mode", "wb") else output.decode("utf-8"))
        stdout.flush()
        calls.append(command)
        return _FakeProc(rc, output)
    return popen, calls


def test_cli_que_muere_al_arrancar_sin_salida_se_reintenta_una_vez(monkeypatch, tmp_path):
    """kimi.exe murió dos veces el 2026-09-17 a 1–2 s del arranque con log
    vacío (rc=1 y 0xC0000005); el turno quedaba en ERROR esperando un clic
    humano por un fallo que no tenía que ver con el prompt."""
    _isolate(monkeypatch, tmp_path)
    popen, calls = _fake_popen([(3221225477, b""), (0, b"POSITION: yes\nlisto")])
    monkeypatch.setattr(relay.subprocess, "Popen", popen)
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)
    seat = {"seat": "melchior", "bin": "/fake/kimi", "args": ["-p"], "journal": "inline"}
    assert relay._run_cli_inline(seat, "prompt", str(tmp_path), 60, token="d1::melchior").endswith("listo")
    assert len(calls) == 2
    events = [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "trigger_fast_crash" in events


def test_cli_que_falla_con_salida_o_dos_veces_no_se_reintenta_mas(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)
    seat = {"seat": "melchior", "bin": "/fake/kimi", "args": ["-p"], "journal": "inline"}
    # con salida: es un fallo real del turno, no un crash de arranque
    popen, calls = _fake_popen([(1, b"error: cuota agotada")])
    monkeypatch.setattr(relay.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="cuota agotada"):
        relay._run_cli_inline(seat, "prompt", str(tmp_path), 60)
    assert len(calls) == 1
    # dos crashes seguidos: se reporta el segundo, no se insiste
    popen, calls = _fake_popen([(1, b""), (1, b"")])
    monkeypatch.setattr(relay.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="rc=1"):
        relay._run_cli_inline(seat, "prompt", str(tmp_path), 60)
    assert len(calls) == 2


def test_las_fuentes_de_memoria_quedan_en_el_dossier_una_vez_por_ronda(fired_magi, monkeypatch):
    """El operador califica después la memoria que vio el consejo: hay que
    saber QUÉ nodos entraron al prompt de esta ronda. Se guarda una vez por
    ronda (las tres cabezas comparten el bloque) y no se repite en el
    siguiente ciclo del relay."""
    import memory_ctx
    monkeypatch.setattr(memory_ctx, "memoria_con_fuentes",
                        lambda t, a=None, thread=None: ("MEM", ["decision:7", "debate_thread:d7"]))
    conn = FakeConn([], decisions=[mk_decision_row(round=2)])
    relay.process_cycle(conn, fresh_state())
    assert len(conn.updates) == 1
    payload, decision_id = conn.updates[0]
    assert decision_id == 42
    assert payload.obj == {"memory_sources": {"round": 2, "ids": ["decision:7", "debate_thread:d7"]}}
    # el dossier en memoria ya lo tiene: otro ciclo no vuelve a escribir
    relay.process_cycle(conn, fresh_state())
    assert len(conn.updates) == 1
    # sin memoria no hay nada que registrar
    monkeypatch.setattr(memory_ctx, "memoria_con_fuentes", lambda t, a=None, thread=None: ("", []))
    conn2 = FakeConn([], decisions=[mk_decision_row()])
    relay.process_cycle(conn2, fresh_state())
    assert not getattr(conn2, "updates", [])


def test_el_turno_inline_registra_tamano_de_prompt_salida_y_memoria(monkeypatch, tmp_path):
    """Para metrics.py: sin esto sólo sabíamos cuánto tardaba una cabeza, no
    cuánto leía ni cuánto escribía."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "_run_cli_inline", lambda seat, prompt, cwd, timeout, token=None: "POSITION: yes\n" + "x" * 500)
    monkeypatch.setattr(relay.board, "record_position", lambda *a, **kw: ({"action": "wait"}, 1))
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    seat = {"seat": "balthasar", "name": "codex", "type": "cli", "journal": "inline", "tools": True,
            "bin": "/fake/bin", "args": ["exec"]}
    relay._run_cli_inline_turn(seat, mk_decision_row(artifact=str(tmp_path)), str(tmp_path), memory="M" * 300)
    done = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()
            if '"trigger_done"' in l][-1]
    assert done["memory_chars"] == 300 and done["output_chars"] == 514
    assert done["prompt_chars"] > 300 and done["rc"] == 0


def test_las_posiciones_de_rondas_anteriores_se_resumen_en_el_journal_de_la_cabeza():
    """En ronda 2+ cada cabeza releía tres posiciones de hasta 6k chars
    (~4.5k tokens). Se resumen a cabeza + cola: el voto y las condiciones
    sobreviven, la argumentación larga no. Las de la ronda actual y los
    mensajes humanos no se tocan."""
    long_old = "ARGUMENTO-VIEJO " * 400 + "\nPOSITION: conditional\nCONDITIONS: probar antes"
    messages = [
        {"id": 10, "author": "melchior", "kind": "posicion", "body": long_old},
        {"id": 11, "author": "adrian", "kind": "contexto", "body": "sigan con esto " * 300},
        {"id": 12, "author": "casper", "kind": "posicion", "body": "ARGUMENTO-ACTUAL " * 300},
    ]

    class Conn:
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("SELECT id, author, kind, body"):
                return _Result(list(reversed(messages)))
            if q.startswith("SELECT message_id, round FROM positions"):
                assert params == (42, 2)
                return _Result([{"message_id": 10, "round": 1}])
            raise AssertionError(q)

    journal = relay._journal_inline(Conn(), "d-42", decision={"id": 42, "round": 2})
    old, human, current = journal
    assert "posición de una ronda anterior, acotada" in old["body"]
    assert old["body"].endswith("CONDITIONS: probar antes") and old["body"].startswith("ARGUMENTO-VIEJO")
    assert len(old["body"]) <= relay.apihead.POSITION_DIGEST_HEAD + relay.apihead.POSITION_DIGEST_TAIL + 60
    assert human["body"] == messages[1]["body"], "los mensajes humanos no se resumen"
    assert current["body"].startswith("ARGUMENTO-ACTUAL") and "acotada" not in current["body"]
    assert "id" not in old, "el prompt no lleva ids de mensajes"
    # sin decisión (chat) o en ronda 1 no se consulta nada
    class NoPositions(Conn):
        def execute(self, query, params=()):
            assert "positions" not in query
            return super().execute(query, params)
    for journal in (relay._journal_inline(NoPositions(), "d-42"),
                    relay._journal_inline(NoPositions(), "d-42", decision={"id": 42, "round": 1})):
        assert journal[0]["body"].startswith("ARGUMENTO-VIEJO") and "ronda anterior" not in journal[0]["body"]


# ------------------------------------------------- ejecutor: causas y reintento

def _executor_env(monkeypatch, tmp_path, outcomes):
    """Ejecutor de mentira: cada Popen consume (rc, salida) de `outcomes`."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "ejec", "name": "x", "type": "cli", "executor": True, "bin": sys.executable, "args": []}])
    monkeypatch.setattr(relay.production, "plan",
                        lambda cwd, did, prev: {"branch": "magi/d9", "base_branch": "main", "base_sha": "base0",
                                                "repo": str(tmp_path), "worktree": str(tmp_path)})
    monkeypatch.setattr(relay.production, "prepare", lambda run: str(tmp_path))
    monkeypatch.setattr(relay.production, "common_dir", lambda repo: str(tmp_path))
    monkeypatch.setattr(relay.production, "commit_execution", lambda run, message: "sha1")
    monkeypatch.setattr(relay.production, "review_target", lambda run: ("sha1", "diff"))
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)
    popen, calls = _fake_popen(outcomes)
    monkeypatch.setattr(relay.subprocess, "Popen", popen)
    fallos = []
    monkeypatch.setattr(relay, "_execution_failed", lambda d, detail, **kw: fallos.append((detail, kw)))
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    return calls, fallos


def test_ejecutor_que_muere_al_arrancar_se_reintenta_y_no_usa_tempdir(monkeypatch, tmp_path):
    """#28: dos ejecuciones marcadas FALLIDAS por WinError 32 al borrar el
    tempdir del prompt con codex todavía dentro, y una por rc=-1 sin salida.
    El prompt vive en logs/executor/<thread>/ (sin limpieza que falle) y un
    crash de arranque se reintenta una vez."""
    calls, fallos = _executor_env(monkeypatch, tmp_path, [(1, b""), (0, b"hecho")])
    monkeypatch.setattr(relay.board, "start_decision", lambda *a, **kw: {"decision_id": 77, "thread": "d77"})
    relay._execute_plan({"id": 9, "thread": "d9", "title": "plan", "minority_report": None}, str(tmp_path))
    assert len(calls) == 2 and fallos == []
    assert (tmp_path / "executor" / "d9" / "prompt.txt").exists()
    events = [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "trigger_fast_crash" in events


def test_fallo_del_ejecutor_lleva_causa_y_cola_del_log(monkeypatch, tmp_path):
    calls, fallos = _executor_env(monkeypatch, tmp_path, [(2, b"linea 1\nerror: sin permisos\n")])
    relay._execute_plan({"id": 9, "thread": "d9", "title": "plan", "minority_report": None}, str(tmp_path))
    assert len(calls) == 1, "con salida no es crash de arranque: no se reintenta"
    detail, kw = fallos[0]
    assert kw["cause"] == "error" and "rc=2" in detail
    assert relay._log_tail(kw["log_path"]).endswith("error: sin permisos")


def test_fallo_de_commit_se_clasifica_sin_commit(monkeypatch, tmp_path):
    calls, fallos = _executor_env(monkeypatch, tmp_path, [(0, b"ok")])

    def boom(run, message):
        raise RuntimeError("los cambios del ejecutor están ignorados y no pueden revisarse")
    monkeypatch.setattr(relay.production, "commit_execution", boom)
    relay._execute_plan({"id": 9, "thread": "d9", "title": "plan", "minority_report": None}, str(tmp_path))
    assert fallos[0][1]["cause"] == "sin_commit"


def test_execution_failed_escribe_causa_y_cola_en_el_journal(monkeypatch, tmp_path):
    written = []

    class Conn(FakeConn):
        def execute(self, query, params=()):
            written.append((" ".join(query.split()), params))
            return _Result([])
    monkeypatch.setattr(relay, "connect", lambda: Conn([]))
    log_file = tmp_path / "execute.log"
    log_file.write_text("\n".join(f"linea {i}" for i in range(100)) + "\nTraceback: boom", encoding="utf-8")
    relay._execution_failed({"id": 9, "thread": "d9"}, "salió rc=1", cause="crash", log_path=log_file)
    update, insert = written
    assert update[1][0].obj["execution_cause"] == "crash" and update[1][0].obj["execution_state"] == "failed"
    body = insert[1][1]
    assert body.startswith("EJECUCIÓN FALLIDA [crash]") and "Traceback: boom" in body and "linea 0" not in body


def test_review_approves_unanime_o_mayoria_autorizada():
    rev = {"id": 30, "ruling": "yes", "confidence": relay.decision.CONFIDENCE_MAJORITY, "minority_report": {}}
    assert not relay._review_approves(rev, None)
    assert relay._review_approves(rev, {"review_id": 30, "by": "adrian"})
    assert not relay._review_approves(rev, {"review_id": 29, "by": "adrian"}), "la autorización es por revisión"
    assert relay._review_approves(dict(rev, confidence=1.0), None)
    assert not relay._review_approves(dict(rev, ruling="no", confidence=1.0), {"review_id": 30})
    assert not relay._review_approves(dict(rev, confidence=1.0, minority_report={"aborted": True}), None)
