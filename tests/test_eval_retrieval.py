"""La evaluación con conversaciones reales tiene partes puras que sí se
prueban sin Postgres ni modelo: qué mensajes valen como consulta, cómo se
oculta un mensaje del nodo (leave-one-out) y cómo se cuentan los aciertos."""

from contextlib import closing
import json
import sqlite3

import db
import eval_retrieval


def test_build_cases_descarta_control_y_reportes_de_resultado():
    rows = [
        {"decision_id": 1, "thread": "d1", "artifact": "C:/repo", "message_id": 10,
         "body": "¿Conviene persistir antes de confirmar la migración de datos?"},
        {"decision_id": 1, "thread": "d1", "artifact": "C:/repo", "message_id": 11, "body": "seguí"},
        {"decision_id": 2, "thread": "d2", "artifact": None, "message_id": 12,
         "body": "Resultado reportado por el usuario: worked. No es verificación independiente."},
        {"decision_id": 2, "thread": "d2", "artifact": None, "message_id": 13, "body": "ok gracias"},
    ]
    cases = eval_retrieval.build_cases(rows)
    assert [c["message_id"] for c in cases] == [10]
    assert cases[0]["expected"] == {"decision:1", "debate_thread:d1"}
    assert cases[0]["artifact"] == "C:/repo"


def test_strip_message_borra_evidencia_y_memoria_explicita_del_mensaje():
    props = {
        "objective": "Migrar datos",
        "evidence": [{"id": 10, "author": "adrian", "body": "persistir antes"},
                     {"id": 9, "author": "casper", "body": "voto"}],
        "explicit_memory": {"current": [{"message_id": 10, "text": "Objetivo: persistir"}],
                            "history": [{"message_id": 10, "text": "Objetivo: persistir"},
                                        {"message_id": 3, "text": "Objetivo: viejo"}]},
    }
    out = eval_retrieval.strip_message(props, 10)
    assert out["objective"] == "Migrar datos"
    assert [e["id"] for e in out["evidence"]] == [9]
    assert out["explicit_memory"] == {"current": [], "history": [{"message_id": 3, "text": "Objetivo: viejo"}]}
    assert props["evidence"][0]["id"] == 10, "no muta el original"


def test_evaluate_oculta_el_mensaje_reindexa_y_mide_recall(graph_db, tmp_path):
    path = tmp_path / "memory.db"
    db.upsert_node(graph_db, id="decision:1", domain="decision", source="t", updated_at="2026",
                   label="#1 Migración", props={"evidence": [{"id": 10, "author": "adrian", "body": "persistir"}]})
    db.upsert_node(graph_db, id="debate_thread:d1", domain="debate_thread", source="t", updated_at="2026",
                   label="d1", props={"evidence": [{"id": 10, "author": "adrian", "body": "persistir"}]})
    graph_db.commit()
    graph_db.close()
    # el fixture ya apuntó settings/db a tmp_path/memory.db

    seen = {"queries": [], "reindexed": [], "hidden": []}

    def retrieve(db_path, query, artifact, semantic):
        with closing(sqlite3.connect(db_path)) as conn:
            props = json.loads(conn.execute("SELECT props FROM nodes WHERE id='decision:1'").fetchone()[0])
        seen["hidden"].append(props["evidence"])
        seen["queries"].append((query, artifact, semantic))
        # léxico no lo encuentra; híbrido lo pone segundo
        return [] if not semantic else [{"id": "otra"}, {"id": "decision:1"}]

    cases = eval_retrieval.build_cases([{"decision_id": 1, "thread": "d1", "artifact": "C:/r",
                                         "message_id": 10, "body": "persistir antes de confirmar la migración"}])
    report = eval_retrieval.evaluate(cases, path, retrieve, lambda p: seen["reindexed"].append(p))

    assert report["cases"] == 1
    assert report["lexical"] == {"recall@1": 0.0, "recall@3": 0.0}
    assert report["hybrid"] == {"recall@1": 0.0, "recall@3": 1.0}
    assert report["results"][0]["hybrid_rank"] == 2
    assert seen["hidden"] == [[], []], "el mensaje se ocultó antes de consultar"
    assert len(seen["reindexed"]) == 1
    assert seen["queries"][0][1] is None, "sin --same-repo no viaja el artifact"
    # la base original no se tocó
    with closing(sqlite3.connect(path)) as conn:
        assert json.loads(conn.execute("SELECT props FROM nodes WHERE id='decision:1'").fetchone()[0])["evidence"]
