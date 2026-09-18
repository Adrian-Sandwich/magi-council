"""Tests del store del grafo: idempotencia, merge de props, cache incremental
y recolección de basura.

El invariante que más importa: correr los ingestors dos veces no puede cambiar
el contenido. Sin eso, `refresh.sh` acumula peso en los edges y duplica cosas
cada vez que se corre.
"""

import db


NOW = "2026-01-01T00:00:00+00:00"


def _node(conn, id):
    return conn.execute(
        "SELECT id, label, tag, domain, props, source FROM nodes WHERE id = ?", (id,)
    ).fetchone()


def test_upsert_node_es_idempotente(graph_db):
    for _ in range(3):
        db.upsert_node(graph_db, id="n1", domain="doc", source="s", updated_at=NOW, label="L")
    assert graph_db.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1


def test_upsert_node_mergea_props_de_distintos_ingestors(graph_db):
    """Un `file:` puede ser tocado por una sesión y citado por un doc. El
    segundo ingestor no puede borrar lo que puso el primero."""
    db.upsert_node(graph_db, id="f1", domain="file", source="a", updated_at=NOW, props={"x": 1})
    db.upsert_node(graph_db, id="f1", domain="file", source="b", updated_at=NOW, props={"y": 2})
    import json
    props = json.loads(_node(graph_db, "f1")[4])
    assert props == {"x": 1, "y": 2}


def test_upsert_edge_no_duplica_ni_acumula_peso(graph_db):
    for _ in range(3):
        db.upsert_edge(graph_db, "a", "b", "touched", source="s", updated_at=NOW, weight=2.0)
    rows = graph_db.execute("SELECT weight FROM edges").fetchall()
    assert rows == [(2.0,)]


def test_reset_source_edges_solo_borra_su_propio_source(graph_db):
    db.upsert_edge(graph_db, "a", "b", "touched", source="claude", updated_at=NOW)
    db.upsert_edge(graph_db, "a", "c", "touched", source="kimi", updated_at=NOW)
    db.reset_source_edges(graph_db, "claude", "a")
    rows = graph_db.execute("SELECT to_id, source FROM edges").fetchall()
    assert rows == [("c", "kimi")]


# ------------------------------------------------------------ cache

def test_cache_devuelve_los_hechos_si_el_archivo_no_cambio(graph_db, tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("linea\n")
    assert db.cached_facts(graph_db, "src", path) is None

    db.store_facts(graph_db, "src", path, {"n": 1})
    assert db.cached_facts(graph_db, "src", path) == {"n": 1}


def test_cache_se_invalida_cuando_el_archivo_crece(graph_db, tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("linea\n")
    db.store_facts(graph_db, "src", path, {"n": 1})

    path.write_text("linea\notra\n")   # los logs son append-only
    assert db.cached_facts(graph_db, "src", path) is None


def test_cache_de_archivo_inexistente_no_explota(graph_db, tmp_path):
    assert db.cached_facts(graph_db, "src", tmp_path / "no-existe") is None
    db.store_facts(graph_db, "src", tmp_path / "no-existe", {"n": 1})  # no-op


def test_forget_missing_files_limpia_lo_borrado(graph_db, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("x"); b.write_text("y")
    db.store_facts(graph_db, "src", a, {})
    db.store_facts(graph_db, "src", b, {})

    assert db.forget_missing_files(graph_db, "src", {str(a)}) == 1
    rows = graph_db.execute("SELECT path FROM file_cache").fetchall()
    assert rows == [(str(a),)]


# ------------------------------------------------------------ gc

def test_sweep_domain_borra_lo_que_ya_no_se_ve(graph_db):
    db.upsert_node(graph_db, id="s1", domain="claude_session", source="c", updated_at=NOW)
    db.upsert_node(graph_db, id="s2", domain="claude_session", source="c", updated_at=NOW)
    db.upsert_node(graph_db, id="p1", domain="project", source="c", updated_at=NOW)
    db.upsert_edge(graph_db, "s2", "p1", "belongs_to", source="c", updated_at=NOW)

    assert db.sweep_domain(graph_db, "claude_session", {"s1"}) == 1
    assert _node(graph_db, "s2") is None
    assert _node(graph_db, "s1") is not None
    # y se lleva los edges que colgaban de la sesión borrada
    assert graph_db.execute("SELECT count(*) FROM edges").fetchone()[0] == 0


def test_sweep_domain_no_toca_otros_dominios(graph_db):
    db.upsert_node(graph_db, id="k1", domain="kimi_session", source="k", updated_at=NOW)
    db.sweep_domain(graph_db, "claude_session", set())
    assert _node(graph_db, "k1") is not None


def test_gc_orphans_borra_archivos_sin_aristas(graph_db):
    db.upsert_node(graph_db, id="file:huerfano", domain="file", source="c", updated_at=NOW)
    db.upsert_node(graph_db, id="file:citado", domain="file", source="c", updated_at=NOW)
    db.upsert_node(graph_db, id="s1", domain="claude_session", source="c", updated_at=NOW)
    db.upsert_edge(graph_db, "s1", "file:citado", "touched", source="c", updated_at=NOW)

    assert db.gc_orphans(graph_db) == 1
    assert _node(graph_db, "file:huerfano") is None
    assert _node(graph_db, "file:citado") is not None


def test_gc_orphans_no_toca_sesiones_aisladas(graph_db):
    """Una sesión sin archivos tocados sigue siendo un hecho del grafo."""
    db.upsert_node(graph_db, id="s1", domain="claude_session", source="c", updated_at=NOW)
    assert db.gc_orphans(graph_db) == 0
    assert _node(graph_db, "s1") is not None


# ------------------------------------------------------------ ingest_debate

def test_write_decision_crea_nodo_con_dossier_y_edge_journal_of(graph_db):
    """ingest_debate.write_decision: nodo decision con ruling/minority/
    mind_changes en props, y edge journal_of desde el thread. Idempotente."""
    import json as _json
    from datetime import datetime, timezone

    import ingest_debate

    row = {
        "id": 7,
        "title": "¿Hubo SQLi?",
        "protocol": "critique",
        "status": "closed",
        "ruling": "yes",
        "confidence": 0.66,
        "minority_report": {
            "minority": [{"head": "casper", "position": "no", "conditions": None}],
            "mind_changes": [{"head": "balthasar", "from": "no", "to": "yes", "round": 2}],
            "degraded": False,
        },
        "thread": "d7",
        "round": 2,
        "created_by": "adrian",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "closed_at": None,
    }
    ingest_debate.write_decision(graph_db, row, NOW)

    node = _node(graph_db, "decision:7")
    assert node is not None
    assert node[3] == "decision"
    props = _json.loads(node[4])
    assert props["ruling"] == "yes"
    assert props["mind_changes"][0]["head"] == "balthasar"
    edge = graph_db.execute(
        "SELECT type FROM edges WHERE from_id = 'debate_thread:d7' AND to_id = 'decision:7'"
    ).fetchone()
    assert edge == ("journal_of",)

    ingest_debate.write_decision(graph_db, row, NOW)
    n = graph_db.execute("SELECT count(*) FROM nodes WHERE domain = 'decision'").fetchone()[0]
    assert n == 1, "dos corridas no duplican el nodo"


def test_record_run_guarda_la_ultima_corrida_por_fuente(graph_db):
    import db

    db.record_run(graph_db, "ingest_claude", finished_at=100.0)
    db.record_run(graph_db, "ingest_claude", finished_at=200.0, summary="3 sesiones")
    db.record_run(graph_db, "ingest_docs", finished_at=150.0)
    assert db.last_runs(graph_db) == {"ingest_claude": 200.0, "ingest_docs": 150.0}
