"""Tests del healthcheck: ninguno toca Postgres ni el heartbeat reales —
los checks se pisan con stubs y sys.argv se controla por monkeypatch."""

import pytest

import healthcheck


def _check_falla():
    return healthcheck.CRIT, "postgres inalcanzable: connection refused"


def _check_ok():
    return healthcheck.OK, "todo bien"


def test_notify_con_check_fallido_no_revienta(monkeypatch):
    """Regresión: la variable local `notify = "--notify" in sys.argv` pisaba
    la función `notify()` del módulo y el aviso explotaba con
    TypeError: 'bool' object is not callable justo cuando hacía falta."""
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_falla)])
    monkeypatch.setattr(healthcheck, "notify", lambda *a: None)
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    assert healthcheck.main() == 2  # no TypeError


def test_notify_avisa_solo_si_hay_fallas(monkeypatch):
    avisos = []
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_falla)])
    monkeypatch.setattr(healthcheck, "notify", lambda title, body: avisos.append((title, body)))
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    healthcheck.main()
    assert len(avisos) == 1
    assert "CRIT" in avisos[0][0]
    assert "postgres" in avisos[0][1]


def test_notify_sin_fallas_no_avisa(monkeypatch):
    avisos = []
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_ok)])
    monkeypatch.setattr(healthcheck, "notify", lambda title, body: avisos.append((title, body)))
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    assert healthcheck.main() == 0
    assert avisos == []


def test_un_check_que_revienta_no_tumba_el_reporte(monkeypatch):
    def _check_roto():
        raise RuntimeError("boom inesperado")

    monkeypatch.setattr(healthcheck, "CHECKS", [("roto", _check_roto), ("ok", _check_ok)])
    monkeypatch.setattr("sys.argv", ["healthcheck.py"])
    assert healthcheck.main() == 2


# ------------------------------------------------------------ memory-graph

def _graph(tmp_path, runs, mtime_age=0):
    """memory.db con la tabla ingest_runs poblada como diga `runs`
    ({source: edad en segundos}); None = sin tabla (base vieja)."""
    import os
    import sqlite3
    import time

    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    if runs is not None:
        conn.execute("CREATE TABLE ingest_runs (source TEXT PRIMARY KEY, finished_at REAL NOT NULL, summary TEXT)")
        conn.executemany("INSERT INTO ingest_runs VALUES (?, ?, NULL)",
                         [(s, time.time() - age) for s, age in runs.items()])
    conn.commit()
    conn.close()
    os.utime(path, (time.time() - mtime_age, time.time() - mtime_age))
    return path


def _heartbeat(tmp_path, age=10):
    import json
    import time

    path = tmp_path / "hb.json"
    path.write_text(json.dumps({"memory_sync": {"status": "ok", "last_success": time.time() - age,
                                                 "semantic_status": "ok"}}))
    return path


def test_graph_detecta_refresh_muerto_aunque_el_relay_toque_la_base(tmp_path, monkeypatch):
    """La regresión del 2026-08-26 volvía a ser invisible: el relay sincroniza
    conversaciones cada 30s y el mtime de memory.db decía "fresco" con las
    sesiones de Claude/Kimi días sin ingestar. Ahora se mira cada fuente."""
    fresh = {"ingest_claude": 30, "ingest_kimi": 30, "ingest_docs": 30, "ingest_code": 30}
    monkeypatch.setattr(healthcheck, "MEMORY_DB",
                        _graph(tmp_path, dict(fresh, ingest_kimi=3 * 24 * 3600)))
    monkeypatch.setattr(healthcheck, "HEARTBEAT_PATH", _heartbeat(tmp_path))
    status, detail = healthcheck.check_graph()
    assert status == healthcheck.CRIT
    assert "ingest_kimi" in detail and "conversaciones sincronizadas" in detail


def test_graph_avisa_fuente_que_nunca_corrio(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "MEMORY_DB",
                        _graph(tmp_path, {"ingest_claude": 30, "ingest_kimi": 30, "ingest_docs": 30}))
    monkeypatch.setattr(healthcheck, "HEARTBEAT_PATH", _heartbeat(tmp_path))
    status, detail = healthcheck.check_graph()
    assert status == healthcheck.WARN
    assert "ingest_code nunca corrió" in detail


def test_graph_ok_cuando_todas_las_fuentes_son_recientes(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "MEMORY_DB", _graph(tmp_path, {
        s: 600 for s in healthcheck.REFRESH_SOURCES}))
    monkeypatch.setattr(healthcheck, "HEARTBEAT_PATH", _heartbeat(tmp_path))
    status, detail = healthcheck.check_graph()
    assert status == healthcheck.OK
    assert "refresh.sh corrió hace" in detail


def test_graph_sin_tabla_de_corridas_no_da_ok_a_ciegas(tmp_path, monkeypatch):
    """Base anterior a ingest_runs: sólo queda el mtime, que ya no distingue
    fuentes; se pide correr refresh.sh en vez de reportar OK."""
    monkeypatch.setattr(healthcheck, "MEMORY_DB", _graph(tmp_path, None))
    monkeypatch.setattr(healthcheck, "HEARTBEAT_PATH", _heartbeat(tmp_path))
    status, detail = healthcheck.check_graph()
    assert status == healthcheck.WARN
    assert "sin registro" in detail
