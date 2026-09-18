"""Tests de la UI MAGI (magi_ui.py): el kanji del veredicto, el snapshot de
estado, el frame SSE y el server HTTP stdlib end-to-end con una conexión y un
board de mentira. Nada toca Postgres ni el registry real."""

import http.client
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import magi_ui


def _decision(id, status="open", ruling=None, round=1, title="¿Ataque?", minority_report=None):
    return {
        "id": id, "title": title, "artifact": "/repo/auth.log",
        "protocol": "critique", "status": status, "ruling": ruling,
        "confidence": 0.66 if ruling else None, "round": round,
        "thread": f"d{id}", "heads": ["melchior", "balthasar", "casper"],
        "minority_report": minority_report,
    }


DECISIONS = [
    _decision(2, status="open", round=2, title="¿Hay SQLi?"),
    _decision(1, status="closed", ruling="yes", title="¿Puerto abierto?"),
]

POSITIONS = [
    {"decision_id": 1, "head": "melchior", "round": 1, "position": "yes", "conditions": None, "body": "sí hay"},
    {"decision_id": 1, "head": "balthasar", "round": 1, "position": "yes", "conditions": None, "body": "afirmo"},
    {"decision_id": 1, "head": "casper", "round": 1, "position": "no", "conditions": ["no hay egress"], "body": "no convence"},
    {"decision_id": 2, "head": "melchior", "round": 1, "position": "yes", "conditions": None, "body": "evidencia"},
    {"decision_id": 2, "head": "balthasar", "round": 1, "position": "no", "conditions": None, "body": "dudas"},
    {"decision_id": 2, "head": "casper", "round": 1, "position": "conditional", "conditions": ["ver egress"], "body": "depende"},
    {"decision_id": 2, "head": "melchior", "round": 2, "position": "yes", "conditions": None, "body": "me mantengo"},
]

MESSAGES = [
    {"thread": "d2", "author": "melchior", "kind": "posicion", "body": "me mantengo",
     "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
    {"thread": "d2", "author": "adrian", "kind": "analisis", "body": "¿Hay SQLi?",
     "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"thread": "d1", "author": "magi", "kind": "resultado", "body": "CERRADA ruling: yes",
     "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
]


class FakeUiConn:
    def __init__(self):
        self.decisions, self.positions, self.messages = DECISIONS, POSITIONS, MESSAGES

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if "FROM decisions WHERE status IN ('open', 'split', 'executing')" in q:
            rows = [d for d in self.decisions if d["status"] in ("open", "split", "executing")]
        elif "FROM decisions WHERE status = 'closed'" in q:
            limit = params[0] if params else len(self.decisions)
            rows = [d for d in self.decisions if d["status"] == "closed"][:limit]
        elif q.startswith("SELECT p.decision_id"):
            rows = [p for p in self.positions if p["decision_id"] in params[0]]
        elif q.startswith("SELECT thread, author, kind, body, created_at"):
            # journal de decisiones: threads dados, filtrado por kinds de
            # conversación (params[1]), en DESC — build_state revierte.
            # La query real acota por thread (row_number por partición,
            # params[2]): el hilo más hablador no se come el journal de
            # las demás.
            threads, kinds = params[0], params[1]
            limite = params[2] if len(params) > 2 else None
            rows = [m for m in self.messages
                    if m["thread"] in threads and m["kind"] in kinds][::-1]
            if limite is not None:
                vistos: dict[str, int] = {}
                acotados = []
                for m in rows:
                    if vistos.get(m["thread"], 0) < limite:
                        vistos[m["thread"]] = vistos.get(m["thread"], 0) + 1
                        acotados.append(m)
                rows = acotados
        elif q.startswith("SELECT id, thread, status FROM decisions"):
            if "WHERE id = %s" in q:
                return _R([d for d in self.decisions if d["id"] == params[0]])
            # la consulta del modo council trae los estados inline en el SQL
            import re as _re
            statuses = _re.findall(r"'(\w+)'", q.split("IN", 1)[1])
            rows = [
                {"id": d["id"], "thread": d["thread"], "status": d["status"]}
                for d in self.decisions if d["status"] in statuses
            ]
        elif q.startswith("SELECT author, kind, body, created_at"):
            # el thread de chat: mensajes de UN thread, los últimos N en DESC
            # (build_state revierte para cronológico)
            rows = [m for m in self.messages if m["thread"] == params[0]][-params[1]:][::-1]
        else:
            raise AssertionError(f"query inesperada: {q}")
        return _R(rows)

    def transaction(self):
        class _Tx:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        return _Tx()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _R:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# ------------------------------------------------------------ verdict_badge

def test_badge_abierto_es_deliberating_paradeando():
    b = magi_ui.verdict_badge({"status": "open", "ruling": None})
    assert b["text"] == "DELIBERATING" and b["flicker"] is True


@pytest.mark.parametrize("stage,label", [
    ("pending", "EXECUTING"), ("failed", "EXECUTION FAILED"),
    ("reviewing", "IN REVIEW"), ("merge_blocked", "MERGE PENDING"),
])
def test_badge_describes_execution_stage(stage, label):
    b = magi_ui.verdict_badge(_decision(1, status="executing", ruling="yes",
                            minority_report={"execution_state": stage}))
    assert b["text"] == label
    if stage in ("failed", "merge_blocked"):
        assert not b["flicker"]


def test_active_production_is_visible_beyond_closed_history_limit():
    conn = FakeUiConn()
    conn.decisions = [_decision(i, status="closed", ruling="yes") for i in range(2, 30)]
    conn.decisions.append(_decision(1, status="executing", ruling="yes"))
    snapshot = magi_ui.build_state(conn)
    assert any(d["id"] == 1 for d in snapshot["decisions"])


def test_badge_split_es_stalemate_gris():
    b = magi_ui.verdict_badge({"status": "split", "ruling": None})
    assert b["text"] == "STALEMATE" and b["color"] == "gray" and b["flicker"] is False


def test_badge_cerrada_usa_el_ruling():
    assert magi_ui.verdict_badge({"status": "closed", "ruling": "yes"})["text"] == "APPROVED"
    assert magi_ui.verdict_badge({"status": "closed", "ruling": "no"})["color"] == "#a41413"
    # cerrada por arbitraje humano (sin ruling de máquina) = STALEMATE
    assert magi_ui.verdict_badge({"status": "closed", "ruling": None})["text"] == "STALEMATE"


# ------------------------------------------------------------ build_state

def test_build_state_marca_votos_de_la_ronda_actual():
    state = magi_ui.build_state(FakeUiConn())
    d2 = state["decisions"][0]
    assert d2["id"] == 2 and d2["status"] == "open"
    assert d2["badge"]["flicker"] is True
    seats = {s["seat"]: s for s in d2["seats"]}
    assert seats["melchior"]["voted"] is True, "melchior recasteó en ronda 2"
    assert seats["melchior"]["body"] == "me mantengo"
    assert seats["balthasar"]["voted"] is False, "su voto es de la ronda 1"
    assert seats["balthasar"]["position"] == "no"
    assert seats["casper"]["conditions"] == ["ver egress"]


def test_build_state_conserva_actividad_persistida_del_ejecutor(tmp_path, monkeypatch):
    activity = {"seat": "balthasar", "pid": 4321,
                "started_at": "2026-09-17T23:00:00+00:00", "turn": "execute"}
    conn = FakeUiConn()
    conn.decisions = [_decision(3, status="executing", ruling="yes",
                                minority_report={"execution_state": "pending",
                                                 "execution_activity": activity})]
    monkeypatch.setattr(magi_ui, "HEARTBEAT_PATH", tmp_path / "missing-heartbeat.json")
    decision = magi_ui.build_state(conn)["decisions"][0]
    assert decision["execution_activity"] == activity


def test_build_state_trae_journal_y_cerradas():
    state = magi_ui.build_state(FakeUiConn())
    d1 = state["decisions"][1]
    assert d1["status"] == "closed" and d1["badge"]["text"] == "APPROVED"
    assert d1["journal"][0]["kind"] == "resultado"
    d2 = state["decisions"][0]
    # la conversación de una decisión son las posiciones, no el título
    # (kind 'analisis' queda fuera: es el encabezado, no un turno)
    assert [m["author"] for m in d2["journal"]] == ["melchior"]


def test_sse_frame_es_data_json_utf8():
    frame = magi_ui.sse_frame({"text": "APPROVED · 承認"})
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    assert "承認".encode("utf-8") in frame, "los caracteres no-ASCII viajan en UTF-8 real, no \\uXXXX"


# ------------------------------------------------------------ server HTTP

# Toda request autenticada manda X-Magi-Token (o ?token= en el SSE). Los
# helpers lo mandan siempre; los tests de seguridad usan _raw_* para
# simular un proceso local sin token.
HEADERS = {"Content-Type": "application/json", "X-Magi-Token": magi_ui.TOKEN}


@pytest.fixture
def ui_server(monkeypatch):
    started = {}

    def fake_start(conn, title, artifact=None, protocol="vote", created_by="adrian",
                   seats=None, production=False):
        if not title:
            raise ValueError("falta el título")
        started.update({"title": title, "artifact": artifact, "protocol": protocol,
                        "production": production})
        return {"decision_id": 99, "thread": "d99", "seats": ["melchior"], "degraded": []}

    monkeypatch.setattr(magi_ui, "connect", lambda: FakeUiConn())
    monkeypatch.setattr(magi_ui.board, "start_decision", fake_start)
    server = magi_ui.ThreadingHTTPServer(("127.0.0.1", 0), magi_ui.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], started
    server.shutdown()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"X-Magi-Token": magi_ui.TOKEN})
    return conn.getresponse()


def _get_raw(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def _get_index(port):
    resp = _get(port, "/")
    return resp, resp.read()


def test_get_index_y_estaticos(ui_server):
    port, _ = ui_server
    for path, ctype in (("/", "text/html"), ("/style.css", "text/css"), ("/app.js", "text/javascript"), ("/sound.js", "text/javascript")):
        resp = _get(port, path)
        assert resp.status == 200, path
        assert ctype in resp.getheader("Content-Type"), path
        assert len(resp.read()) > 100


def test_get_index_inyecta_el_token_de_sesion(ui_server):
    """El token se imprime en consola y se inyecta en el HTML: la SPA lo
    lee de window.MAGI_TOKEN y lo devuelve en cada request."""
    port, _ = ui_server
    resp, body = _get_index(port)
    assert resp.status == 200
    assert b"__MAGI_TOKEN__" not in body, "el placeholder se reemplaza al servir"
    assert magi_ui.TOKEN.encode() in body


def test_get_state_devuelve_el_snapshot(ui_server):
    port, _ = ui_server
    resp = _get(port, "/state")
    assert resp.status == 200
    state = json.loads(resp.read())
    assert [d["id"] for d in state["decisions"]] == [2, 1]


def test_get_events_envia_frame_inicial(ui_server):
    port, _ = ui_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/events?token={magi_ui.TOKEN}")
    resp = conn.getresponse()
    assert resp.status == 200
    assert "text/event-stream" in resp.getheader("Content-Type")
    chunk = resp.read1(1 << 20)
    assert chunk.startswith(b"data: ")
    assert "APPROVED".encode("utf-8") in chunk
    conn.close()


# ------------------------------------------------------------ token de sesión

def test_post_sin_token_devuelve_403(ui_server, monkeypatch):
    """Cualquier proceso local podía manejar el consejo: sin el token de
    sesión, los POST se rechazan antes de tocar el board."""
    port, started = ui_server
    monkeypatch.setattr(
        magi_ui.board, "start_decision",
        lambda *a, **kw: pytest.fail("sin token no tiene que llegar al board"),
    )
    for path, payload in (("/start", {"title": "x"}),
                          ("/message", {"mode": "message", "body": "hola"}),
                          ("/abort", {"decision_id": 1}),
                          ("/continuation", {"decision_id": 1, "action": "stop"})):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 403, path
        resp.read()
    assert started == {}


def test_post_con_token_falso_devuelve_403(ui_server):
    port, _ = ui_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/message", json.dumps({"mode": "message", "body": "hola"}),
                 {"Content-Type": "application/json", "X-Magi-Token": "bogus"})
    resp = conn.getresponse()
    assert resp.status == 403
    resp.read()


def test_get_fs_state_y_events_sin_token_devuelven_403(ui_server_conn):
    port, _, _ = ui_server_conn
    assert _get_raw(port, "/fs").status == 403
    assert _get_raw(port, "/state").status == 403
    assert _get_raw(port, f"/events?token=bogus").status == 403
    resp = _get_raw(port, "/events")
    assert resp.status == 403
    resp.read()


def test_post_start_abre_decision_y_propaga_errores(ui_server):
    port, started = ui_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/start", json.dumps({
        "title": "¿Hubo ataque?", "artifact": "/a.log", "protocol": "adaptive",
    }), HEADERS)
    resp = conn.getresponse()
    assert resp.status == 201
    body = json.loads(resp.read())
    assert body["decision_id"] == 99
    assert started["protocol"] == "adaptive"
    assert started["production"] is False, "production no se infiere del artifact"

    conn.request("POST", "/start", json.dumps({"title": ""}), HEADERS)
    resp = conn.getresponse()
    assert resp.status == 400
    assert "error" in json.loads(resp.read())


def test_post_start_con_flag_production_explicito(ui_server):
    """production es un flag booleano del JSON: con artifact + production
    true llega a start_decision como production."""
    port, started = ui_server
    resp = _post(port, "/start", {"title": "deploy", "artifact": "/repo", "production": True})
    assert resp.status == 201
    assert started["production"] is True


def test_post_start_con_body_mal_codificado_devuelve_400(ui_server):
    """Prueba manual del 2026-09-12: un cliente que manda el JSON en latin1
    (ó = 0xF3) explotaba json.loads en el handler y cortaba la conexión sin
    respuesta. Hoy es un 400 como cualquier otro JSON inválido."""
    port, started = ui_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/start", b'{"title": "Decisi\xF3n"}', HEADERS)
    resp = conn.getresponse()
    assert resp.status == 400
    assert "error" in json.loads(resp.read())
    assert started == {}, "no se abrió ninguna decisión con un body roto"


# ------------------------------------------------------------ chat (POST /message)

def _post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(payload), HEADERS)
    return conn.getresponse()


def test_post_message_mode_decision_abre_con_el_texto_libre(ui_server, monkeypatch):
    port, started = ui_server
    resp = _post(port, "/message", {"mode": "decision", "body": "¿arrancamos con el plan?"})
    assert resp.status == 201
    body = json.loads(resp.read())
    assert body["decision_id"] == 99
    assert body["kind"] == "decision"
    assert started["title"] == "¿arrancamos con el plan?"
    assert started["protocol"] == "adaptive", "el chat vota con el protocolo completo"


def test_post_message_mode_decision_acepta_protocolo_explicito(ui_server):
    port, started = ui_server
    resp = _post(port, "/message", {"mode": "decision", "body": "x", "protocol": "vote"})
    assert resp.status == 201
    assert started["protocol"] == "vote"


def test_post_message_a_thread_libre_manda_analisis(ui_server, monkeypatch):
    port, _ = ui_server
    calls = {}

    def fake_human(conn, thread, body):
        calls.update({"thread": thread, "body": body})
        return {"id": 5, "kind": "analisis", "arbitrated_decision": None}

    monkeypatch.setattr(magi_ui.board, "human_message", fake_human)
    resp = _post(port, "/message", {"mode": "message", "thread": "chat", "body": "hola consejo"})
    assert resp.status == 201
    body = json.loads(resp.read())
    assert body["kind"] == "analisis"
    assert calls == {"thread": "chat", "body": "hola consejo"}


def test_post_message_sin_thread_cae_al_chat(ui_server, monkeypatch):
    port, _ = ui_server
    calls = {}
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda conn, thread, body: calls.update({"thread": thread}) or {"id": 1, "kind": "analisis"},
    )
    resp = _post(port, "/message", {"mode": "message", "body": "hola"})
    assert resp.status == 201
    assert calls["thread"] == "chat"


def test_post_message_rechaza_body_vacio(ui_server, monkeypatch):
    port, _ = ui_server
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda *a, **kw: pytest.fail("no tiene que llegar a human_message"),
    )
    resp = _post(port, "/message", {"mode": "message", "body": "   "})
    assert resp.status == 400


def test_build_state_incluye_el_chat():
    conn = FakeUiConn()
    conn.messages = [
        {"thread": "chat", "author": "adrian", "kind": "analisis", "body": "¿qué opinan?",
         "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"thread": "chat", "author": "melchior", "kind": "respuesta", "body": "yo digo que sí",
         "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
    ]
    state = magi_ui.build_state(conn)
    assert [m["author"] for m in state["chat"]] == ["adrian", "melchior"]
    assert state["chat"][1]["body"] == "yo digo que sí"


@pytest.fixture
def ui_server_conn(monkeypatch, tmp_path):
    """Igual que ui_server, pero exponiendo el FakeUiConn para simular el
    estado del tablero (decisiones abiertas/split/cerradas)."""
    started = {}

    def fake_start(conn, title, artifact=None, protocol="vote", created_by="adrian",
                   seats=None, production=False):
        if not title:
            raise ValueError("falta el título")
        started.update({"title": title, "artifact": artifact, "protocol": protocol,
                        "production": production})
        return {"decision_id": 99, "thread": "d99", "seats": ["melchior"], "degraded": []}

    conn = FakeUiConn()
    monkeypatch.setattr(magi_ui, "connect", lambda: conn)
    monkeypatch.setattr(magi_ui.board, "start_decision", fake_start)
    server = magi_ui.ThreadingHTTPServer(("127.0.0.1", 0), magi_ui.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], started, conn
    server.shutdown()


def test_council_con_decision_abierta_manda_contexto(ui_server_conn, monkeypatch):
    """La caja única: en modo council el sistema elige el destino. Con una
    decisión abierta, el mensaje del operador es CONTEXTO para las cabezas."""
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(2, status="open", round=1)]
    calls = {}

    def fake_human(c, thread, body):
        calls.update(thread=thread, body=body)
        return {"id": 5, "kind": "contexto", "arbitrated_decision": None}

    monkeypatch.setattr(magi_ui.board, "human_message", fake_human)
    resp = _post(port, "/message", {"mode": "council", "body": "mirá también config.py"})
    assert resp.status == 201
    body = json.loads(resp.read())
    assert body["action"] == "context"
    assert body["decision_id"] == 2
    assert calls == {"thread": "d2", "body": "mirá también config.py"}


@pytest.mark.parametrize("status", ["open", "split", "executing"])
def test_council_usa_la_decision_seleccionada(ui_server_conn, monkeypatch, status):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(8), _decision(2, status=status)]
    calls = []
    def human(c, thread, body):
        calls.append(thread)
        return {"id": 99, "kind": "contexto"}
    monkeypatch.setattr(magi_ui.board, "human_message", human)
    response = _post(port, "/message", {
        "mode": "council", "body": "seguí con los tests", "decision_id": 2,
    })
    assert response.status == 201
    assert json.loads(response.read())["decision_id"] == 2
    assert calls == ["d2"]


def test_council_closed_decision_continues_same_thread(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(8), _decision(2, status="closed", ruling="yes")]
    calls = {}
    monkeypatch.setattr(
        magi_ui.board, "follow_up_decision",
        lambda c, decision_id, body: calls.update(decision_id=decision_id, body=body) or {
            "id": 77, "decision_id": decision_id, "thread": "d2", "action": "follow_up"
        },
    )
    response = _post(port, "/message", {
        "mode": "council", "body": "contexto", "decision_id": 2, "action": "followup",
    })
    assert response.status == 201
    assert json.loads(response.read())["action"] == "follow_up"
    assert calls == {"decision_id": 2, "body": "contexto"}


def test_council_merged_follow_up_focuses_linked_decision(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(28, status="closed", ruling="yes")]
    monkeypatch.setattr(
        magi_ui.board, "follow_up_decision",
        lambda c, decision_id, body: {
            "id": None, "decision_id": 33, "source_decision_id": decision_id,
            "thread": "d33", "action": "opened_follow_up", "production": False,
        },
    )
    response = _post(port, "/message", {
        "mode": "council", "body": "qué sigue?", "decision_id": 28,
        "action": "followup",
    })
    result = json.loads(response.read())
    assert response.status == 201
    assert result["action"] == "opened_follow_up"
    assert result["decision_id"] == 33
    assert result["source_decision_id"] == 28


def test_council_closed_approved_evolves_to_execution(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(2, status="closed", ruling="conditional")]
    calls = {}
    monkeypatch.setattr(
        magi_ui.board, "execute_approved_decision",
        lambda c, decision_id, body: calls.update(decision_id=decision_id, body=body) or {
            "id": 78, "decision_id": decision_id, "thread": "d2",
            "action": "execution_requested",
        },
    )
    response = _post(port, "/message", {
        "mode": "council", "body": "vamos con tu plan", "decision_id": 2,
        "action": "execute",
    })
    assert response.status == 201
    assert json.loads(response.read())["action"] == "execution_requested"
    assert calls == {"decision_id": 2, "body": "vamos con tu plan"}


def test_council_review_execution_targets_original_decision(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(30, status="closed", ruling="conditional")]
    monkeypatch.setattr(
        magi_ui.board, "execute_approved_decision",
        lambda c, decision_id, body: {
            "id": 78, "decision_id": 28, "review_id": decision_id,
            "thread": "d28", "action": "corrections_requested",
        },
    )
    response = _post(port, "/message", {
        "mode": "council", "body": "apruebo tu plan", "decision_id": 30,
        "action": "execute",
    })
    result = json.loads(response.read())
    assert response.status == 201
    assert result["action"] == "corrections_requested"
    assert result["decision_id"] == 28
    assert result["review_id"] == 30


def test_council_con_stalemate_arbitra(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(3, status="split", round=3)]
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda c, thread, body: {"id": 6, "kind": "arbitraje", "arbitrated_decision": 3},
    )
    resp = _post(port, "/message", {"mode": "council", "body": "cierro: sí, con condiciones"})
    body = json.loads(resp.read())
    assert body["action"] == "arbitrated"
    assert body["decision_id"] == 3


def test_council_sin_decision_abierta_abre_una_nueva(ui_server_conn):
    port, started, conn = ui_server_conn
    conn.decisions = [_decision(1, status="closed", ruling="yes")]
    resp = _post(port, "/message", {"mode": "council", "body": "¿y ahora?"})
    body = json.loads(resp.read())
    assert body["action"] == "opened"
    assert body["decision_id"] == 99
    assert started["title"] == "¿y ahora?"
    assert started["protocol"] == "adaptive"


def test_council_con_peticion_de_implementacion_evoluciona_a_production(ui_server_conn):
    """La caja única infiere ejecución de una petición explícita; no hay
    checkbox que el operador deba anticipar antes de conversar."""
    port, started, conn = ui_server_conn
    conn.decisions = [_decision(1, status="closed", ruling="yes")]
    resp = _post(port, "/message", {
        "mode": "council", "body": "implementar el parser",
        "artifact": "C:/src/otro-repo",
    })
    body = json.loads(resp.read())
    assert resp.status == 201
    assert body["action"] == "opened"
    assert body["production"] is True
    assert started["artifact"] == "C:/src/otro-repo"
    assert started["production"] is True


def test_badge_de_abortada_es_aborted_gris():
    d = _decision(5, status="closed", ruling="yes", minority_report={"aborted": True})
    assert magi_ui.verdict_badge(d)["text"] == "ABORTED"


def test_retry_failed_turns_endpoint(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    calls = []
    def retry(db, did, errors):
        calls.append((did, errors))
        return {'decision_id': did, 'action': 'retried'}
    monkeypatch.setattr(magi_ui.turn_errors, 'retry', retry)
    resp = _post(port, '/retry-turns', {'decision_id': 28, 'errors': {'melchior': 'attempt-1'}})
    assert resp.status == 200
    assert json.loads(resp.read())['action'] == 'retried'
    assert calls == [(28, {'melchior': 'attempt-1'})]


def test_continuation_endpoint_opens_linked_decision(ui_server_conn, monkeypatch):
    port, _, _ = ui_server_conn
    calls = []
    def continue_from_proposal(db, did, action):
        calls.append((did, action))
        return {'action': 'opened_follow_up', 'decision_id': 33,
                'source_decision_id': did, 'production': True}
    monkeypatch.setattr(magi_ui.board, 'continue_from_proposal', continue_from_proposal)
    resp = _post(port, '/continuation', {'decision_id': 28, 'action': 'execute'})
    assert resp.status == 201
    assert json.loads(resp.read())['decision_id'] == 33
    assert calls == [(28, 'execute')]


def test_post_abort_cierra_la_decision(ui_server_conn, monkeypatch):
    """El botón ABORT: POST /abort cierra la decisión vía board.abort_decision
    y reporta el thread."""
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(2, status="open")]
    calls = {}

    def fake_abort(c, decision_id):
        calls["id"] = decision_id
        return {"id": decision_id, "thread": "d2", "title": "x"}

    monkeypatch.setattr(magi_ui.board, "abort_decision", fake_abort)
    resp = _post(port, "/abort", {"decision_id": 2})
    body = json.loads(resp.read())
    assert resp.status == 200
    assert body == {"aborted": 2, "thread": "d2"}
    assert calls["id"] == 2


def test_council_con_repo_sin_flag_no_es_production(ui_server_conn):
    """El server NO infiere production desde el artifact: el flag tiene que
    venir explícito en el JSON. Antes, cualquier pregunta con repo era un
    plan auto-ejecutable que mergea en git al aprobarse."""
    port, started, conn = ui_server_conn
    conn.decisions = [_decision(1, status="closed", ruling="yes")]
    resp = _post(port, "/message", {"mode": "council", "body": "hacer X",
                                    "artifact": "C:/repo"})
    body = json.loads(resp.read())
    assert resp.status == 201
    assert body["production"] is False
    assert started["production"] is False
    assert started["artifact"] == "C:/repo"


def test_fs_lista_carpetas_y_marca_repos(ui_server_conn, tmp_path, monkeypatch):
    """El mini-explorador: lista subcarpetas y marca las que tienen .git.
    Anclado al home del usuario: acá el home es tmp_path (patcheado)."""
    (tmp_path / "proyecto-a").mkdir()
    (tmp_path / "proyecto-b").mkdir()
    (tmp_path / "proyecto-b" / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    port, _, _ = ui_server_conn
    resp = _get(port, f"/fs?path={tmp_path}")
    assert resp.status == 200
    data = json.loads(resp.read())
    assert data["path"] == str(tmp_path)
    assert data["parent"] is None, "en la raíz del home no hay 'subir'"
    marcas = {d["name"]: d["git"] for d in data["dirs"]}
    assert marcas == {"proyecto-a": False, "proyecto-b": True}


def test_fs_rechaza_paths_fuera_del_home(ui_server_conn, tmp_path, monkeypatch):
    """/?path= lista CUALQUIER carpeta del disco: ahora el path resuelto
    tiene que quedar dentro del home o es 403 (.. y symlinks incluidos)."""
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    port, _, _ = ui_server_conn
    for pedido in (str(fuera), str(tmp_path / "home" / ".." / "fuera")):
        resp = _get(port, f"/fs?path={pedido}")
        assert resp.status == 403, pedido
        assert "fuera del home" in json.loads(resp.read())["error"]


def test_segui_solo_en_open_responde_hint_sin_gastar_turno(ui_server_conn, monkeypatch):
    """'seguí' solo fuera de STALEMATE no inserta mensaje ni abre decisión:
    responde con un hint (las cabezas no recastan sobre una palabra vacía)."""
    port, started, conn = ui_server_conn
    conn.decisions = [_decision(2, status="open")]
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda *a, **kw: pytest.fail("no tiene que llegar a human_message"),
    )
    monkeypatch.setattr(
        magi_ui.board, "start_decision",
        lambda *a, **kw: pytest.fail("no tiene que abrir una decisión 'seguí'"),
    )
    resp = _post(port, "/message", {"mode": "council", "body": "seguí"})
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["action"] == "hint"
    assert "#2" in body["message"]


def test_segui_solo_sin_nada_abierto_responde_hint(ui_server_conn, monkeypatch):
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(1, status="closed", ruling="yes")]
    monkeypatch.setattr(
        magi_ui.board, "start_decision",
        lambda *a, **kw: pytest.fail("no tiene que abrir una decisión 'seguí'"),
    )
    resp = _post(port, "/message", {"mode": "council", "body": "seguí!"})
    body = json.loads(resp.read())
    assert body["action"] == "hint"
    assert "STALEMATE" in body["message"]


def test_segui_con_contexto_en_open_si_es_contexto(ui_server_conn, monkeypatch):
    """'seguí con el parser' SÍ es contexto legítimo: llega a human_message."""
    port, _, conn = ui_server_conn
    conn.decisions = [_decision(2, status="open")]
    calls = {}
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda c, thread, body: calls.update(thread=thread, body=body) or
        {"id": 1, "kind": "contexto", "arbitrated_decision": None},
    )
    resp = _post(port, "/message", {"mode": "council", "body": "seguí con el parser"})
    assert resp.status == 201
    assert calls == {"thread": "d2", "body": "seguí con el parser"}


def test_force_new_abre_decision_nueva_aunque_haya_una_abierta(ui_server_conn, monkeypatch):
    """El botón NEW: con una decisión abierta, force_new salta la heurística
    y abre otra nueva con el texto (en vez de mandarlo como contexto)."""
    port, started, conn = ui_server_conn
    conn.decisions = [_decision(2, status="open")]
    monkeypatch.setattr(
        magi_ui.board, "human_message",
        lambda *a, **kw: pytest.fail("force_new no tiene que llegar a human_message"),
    )
    resp = _post(port, "/message", {"mode": "council", "body": "otra consulta aparte",
                                    "force_new": True})
    body = json.loads(resp.read())
    assert resp.status == 201
    assert body["action"] == "opened"
    assert body["decision_id"] == 99
    assert started["title"] == "otra consulta aparte"


# ------------------------------------------------------------ ruling desconocido

def test_badge_ruling_desconocido_no_explota_y_broadcast_sobrevive():
    """Fila tocada a mano / tipo de ruling futuro: .get con fallback en vez
    de KeyError — el crash anterior mataba el push SSE para todos los
    clientes (pasaba adentro de _broadcast(build_state(...)) del listener)."""
    d = _decision(7, status="closed", ruling="corregido-a-mano")
    b = magi_ui.verdict_badge(d)
    assert b["text"] == "ERROR" and b["color"] == "gray" and b["flicker"] is False
    magi_ui._broadcast({"decisions": [magi_ui.verdict_badge(d)]})


# ------------------------------------------------------------ journal por thread

def test_build_state_journal_acotado_por_thread():
    """Un thread chatty no se come el journal de los demás: el límite es
    por thread (row_number), no global."""
    conn = FakeUiConn()
    conn.messages = (
        [{"thread": "d2", "author": "melchior", "kind": "posicion", "body": f"m{i:02d}",
          "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for i in range(15)]
        + [{"thread": "d1", "author": "magi", "kind": "resultado", "body": "cierre",
            "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}]
    )
    state = magi_ui.build_state(conn)
    por_id = {d["id"]: d for d in state["decisions"]}
    assert [m["body"] for m in por_id[1]["journal"]] == ["cierre"], \
        "las 15 de d2 no debieron acapar el límite global"
    assert [m["body"] for m in por_id[2]["journal"]] == [f"m{i:02d}" for i in range(3, 15)], \
        "quedan las últimas JOURNAL_MESSAGES en orden cronológico"


# ------------------------------------------------------------ body de POST

def test_post_body_gigante_devuelve_413(ui_server):
    """El body JSON tiene tope (64 KB): un Content-Length enorme se rechaza
    sin leer nada, no se aguanta en memoria."""
    port, _ = ui_server
    resp = _post(port, "/message", {"mode": "message", "body": "x" * (70 * 1024)})
    assert resp.status == 413


def test_post_content_length_roto_devuelve_400(ui_server):
    """Content-Length no numérico: era una excepción fuera del try que
    dejaba el handler sin responder; hoy es un 400."""
    port, _ = ui_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/message")
    conn.putheader("Content-Length", "doce")
    conn.putheader("X-Magi-Token", magi_ui.TOKEN)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 400
    assert "error" in json.loads(resp.read())


# ------------------------------------------------------------ fan-out SSE

def _cliente_registrado():
    q = queue.Queue(maxsize=magi_ui.SSE_QUEUE_MAX)
    with magi_ui.CLIENTS_LOCK:
        magi_ui.CLIENTS.append(q)
    return q


def _cliente_baja(q):
    with magi_ui.CLIENTS_LOCK:
        if q in magi_ui.CLIENTS:
            magi_ui.CLIENTS.remove(q)


def test_push_sin_clientes_no_construye_estado(monkeypatch):
    """Con cero clientes conectados no se gasta un build_state (varias
    queries) en cada NOTIFY ni en cada poll."""
    with magi_ui.CLIENTS_LOCK:
        previos = list(magi_ui.CLIENTS)
        magi_ui.CLIENTS.clear()
    try:
        monkeypatch.setattr(magi_ui, "build_state",
                            lambda conn: pytest.fail("sin clientes no se construye nada"))
        magi_ui._push(object())
    finally:
        with magi_ui.CLIENTS_LOCK:
            magi_ui.CLIENTS.extend(previos)


def test_broadcast_no_reenvia_frame_identico():
    """Frame byte-idéntico al último enviado: no se reenvía (el poll de
    POLL_SECS queda en paz cuando el tablero no cambió)."""
    q = _cliente_registrado()
    try:
        estado = {"decisions": [{"id": "test-dedup"}]}
        magi_ui._broadcast(estado)
        assert q.qsize() == 1
        magi_ui._broadcast(estado)
        assert q.qsize() == 1, "frame duplicado: no tiene que reenviarse"
        magi_ui._broadcast({"decisions": [{"id": "test-dedup-2"}]})
        assert q.qsize() == 2
    finally:
        _cliente_baja(q)


def test_broadcast_descarta_cliente_con_cola_llena(capfd):
    """Cola llena = cliente colgado (laptop dormida, tab congelada): se lo
    larga de CLIENTS con un log en vez de acumular frames para siempre."""
    q = _cliente_registrado()
    for _ in range(magi_ui.SSE_QUEUE_MAX):
        q.put_nowait(b"frame viejo")
    try:
        magi_ui._broadcast({"decisions": [{"id": "test-drop"}]})
        with magi_ui.CLIENTS_LOCK:
            assert q not in magi_ui.CLIENTS
        assert "descartado" in capfd.readouterr().err
    finally:
        _cliente_baja(q)
