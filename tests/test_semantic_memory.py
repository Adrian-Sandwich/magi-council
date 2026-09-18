import json
import os
from pathlib import Path

import pytest
import db
import memory_ctx
import semantic_memory as semantic


def node(conn, identifier, label, **props):
    db.upsert_node(conn, id=identifier, label=label, domain='decision', source='test',
                   updated_at='2026-01-01', props=props)
    conn.commit()


def test_index_updates_changes_removes_deleted_nodes_and_skips_unchanged(graph_db, monkeypatch):
    calls = []
    class Encoder:
        def embed(self, texts):
            calls.extend(texts)
            return [[1, 0] for text in texts]
    monkeypatch.setattr(semantic, 'encoder', lambda *a: Encoder())
    path = Path(graph_db.execute('PRAGMA database_list').fetchone()[2])
    node(graph_db, 'a', 'Acceso')
    assert semantic.index(path) == 1
    assert semantic.index(path) == 0
    node(graph_db, 'a', 'Sesiones')
    assert semantic.index(path) == 1
    graph_db.execute("DELETE FROM nodes WHERE id='a'")
    graph_db.commit()
    assert semantic.index(path) == 0
    assert graph_db.execute('SELECT count(*) FROM semantic_vectors').fetchone()[0] == 0
    assert calls == ['Acceso', 'Sesiones']


def test_semantic_match_respects_repository_and_rejects_stale_vectors(graph_db, monkeypatch):
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    node(graph_db, 'a', 'Acceso', artifact='C:/one')
    node(graph_db, 'b', 'Acceso', artifact='C:/two')
    node(graph_db, 'c', 'Cambió')
    monkeypatch.setattr(semantic, 'scores', lambda *_: {
        'a': (.9, semantic.digest(semantic.documents('Acceso', {'artifact': 'C:/one'}))),
        'b': (.99, semantic.digest(semantic.documents('Acceso', {'artifact': 'C:/two'}))),
        'c': (.99, 'stale'),
    })
    hits = memory_ctx.retrieve('autenticación', artifact='C:/one')
    assert [hit['id'] for hit in hits] == ['a']
    assert memory_ctx.retrieve('autenticación')[0]['reason'] == 'semantic match'


def test_missing_semantic_index_preserves_lexical_search(graph_db, monkeypatch):
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    node(graph_db, 'a', 'Autenticación')
    assert memory_ctx.retrieve('Autenticación')[0]['id'] == 'a'


# Explicit opt-in: normal tests neither download models nor run inference.
@pytest.mark.skipif(os.environ.get('CLAMI_SEMANTIC_EVAL') != '1', reason='local model evaluation opt-in')
def test_real_multilingual_retrieval(graph_db, monkeypatch):
    path = Path(graph_db.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(memory_ctx, 'DB_PATH', path)
    examples = [
        ('login', 'Los usuarios no pueden iniciar sesión', 'Falla la autenticación'),
        ('backup', 'Guardar una copia de seguridad de los datos', 'Necesitamos respaldar la información'),
        ('sound', 'Los efectos de audio están silenciados', 'No se escucha nada'),
        ('memory', 'Conservar el contexto de conversaciones anteriores', 'Recordar lo que hablamos antes'),
        ('speed', 'Reducir el tiempo que tarda en responder la aplicación', 'Mejorar la velocidad del programa'),
        ('access', 'La interfaz debe poder usarse sin ratón', 'Navegación mediante teclado'),
    ]
    for identifier, document, query in examples:
        node(graph_db, identifier, document)
    semantic.index(path)
    semantic_hits = lexical_hits = 0
    for identifier, document, query in examples:
        monkeypatch.setenv('MEMORY_SEMANTIC', '0')
        lexical = memory_ctx.retrieve(query)
        lexical_hits += bool(lexical and lexical[0]['id'] == identifier)
        monkeypatch.setenv('MEMORY_SEMANTIC', '1')
        hits = memory_ctx.retrieve(query)
        semantic_hits += bool(hits and hits[0]['id'] == identifier)
    print(f'Recall@1: lexical={lexical_hits}/{len(examples)}, hybrid={semantic_hits}/{len(examples)}')
    assert semantic_hits >= 5
    assert semantic_hits > lexical_hits
    assert memory_ctx.retrieve('Receta de pastel de chocolate') == []


def test_vectors_are_stored_as_float32_and_legacy_json_rows_still_score(graph_db, monkeypatch):
    """Los vectores en JSON pesaban ~65 KB por nodo y cada consulta los
    parseaba enteros en Python puro. Ahora van como float32 crudo y la
    similitud es una multiplicación de matrices; las filas viejas en JSON
    siguen leyéndose hasta que el índice las reemplace."""
    import sqlite3

    class Encoder:
        def embed(self, texts):
            return [[1, 0] for _ in texts]
    monkeypatch.setattr(semantic, 'encoder', lambda *a: Encoder())
    path = Path(graph_db.execute('PRAGMA database_list').fetchone()[2])
    node(graph_db, 'nuevo', 'Acceso')
    node(graph_db, 'viejo', 'Sesiones')
    assert semantic.index(path) == 2
    raw = graph_db.execute("SELECT vectors FROM semantic_vectors WHERE id='nuevo'").fetchone()[0]
    assert isinstance(raw, bytes) and len(raw) == 2 * 4
    # una fila del formato anterior: JSON con dos pasajes, el segundo es el que matchea
    graph_db.execute("UPDATE semantic_vectors SET vectors=? WHERE id='viejo'",
                     (json.dumps([[0.0, 1.0], [0.6, 0.8]]),))
    graph_db.commit()

    monkeypatch.setattr(semantic, 'query_vector', lambda q: [1.0, 0.0])
    monkeypatch.setenv('MEMORY_SEMANTIC', '1')
    conn = sqlite3.connect(path)
    scores = semantic.scores(conn, 'acceso')
    assert scores['nuevo'][0] == pytest.approx(1.0)
    assert scores['viejo'][0] == pytest.approx(0.6)   # el máximo entre sus pasajes
    assert set(scores) == {'nuevo', 'viejo'}
    # vectores de otra dimensión (otro modelo) no rompen la consulta
    graph_db.execute("UPDATE semantic_vectors SET vectors=? WHERE id='viejo'", (json.dumps([[1.0, 0.0, 0.0]]),))
    graph_db.commit()
    assert set(semantic.scores(conn, 'acceso')) == {'nuevo'}
