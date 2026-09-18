"""Behavioral memory evaluations: relevance, continuity, attribution and bounds."""
import apihead
import db
import memory_ctx


def seed(conn, identifier, label, **props):
    db.upsert_node(conn, id=identifier, label=label, domain='decision', source='test',
                   updated_at='2030-01-01', props=props)
    conn.commit()


def test_relevance_excludes_unrelated_recent_decisions(graph_db, monkeypatch):
    monkeypatch.setattr(memory_ctx, 'DB_PATH', graph_db.execute('PRAGMA database_list').fetchone()[2])
    # DB_PATH is deliberately a Path, as in production.
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(memory_ctx.DB_PATH))
    seed(graph_db, 'decision:1', 'Navier Stokes', ruling='yes')
    seed(graph_db, 'decision:23', 'Dios como concepto', ruling='info',
         evidence=[{'id': 123, 'author': 'casper', 'kind': 'posicion', 'body': 'Una creencia tiene efectos sociales.'}])
    memory = memory_ctx.memoria_para('¿Dios es tangible?')
    assert 'Dios' in memory and 'message:123' in memory and 'efectos sociales' in memory
    assert 'Navier' not in memory
    assert 'confianza 1' not in memory


def test_exact_repository_scope_and_source_time(graph_db, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    seed(graph_db, 'decision:1', 'Parser', artifact='C:/work/app', last_at='2026-01-01')
    seed(graph_db, 'decision:2', 'Parser', artifact='C:/other/app', last_at='2026-03-01')
    seed(graph_db, 'decision:3', 'Parser', artifact='C:/work/app', last_at='2026-02-01')
    hits = memory_ctx.retrieve('Parser', 'C:\\work\\app')
    assert [hit['id'] for hit in hits] == ['decision:3', 'decision:1']


def test_same_thread_survives_topic_change_and_follows_graph_edge(graph_db, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    seed(graph_db, 'decision:7', 'Authentication', thread='d7')
    db.upsert_node(graph_db, id='file:auth', domain='file', source='test',
                   updated_at='2026-01-01', label='auth.py')
    db.upsert_edge(graph_db, 'decision:7', 'file:auth', 'touches', source='test', updated_at='2026-01-01')
    graph_db.commit()
    memory = memory_ctx.memoria_para('ahora verifica las consecuencias', thread='d7')
    assert 'Authentication' in memory and 'auth.py' in memory and 'touches via decision:7' in memory


def test_chat_uses_latest_human_question(monkeypatch):
    calls = []
    monkeypatch.setattr(memory_ctx, 'memoria_para',
                        lambda query, artifact=None, thread=None: calls.append((query, artifact, thread)) or 'MEMORY-SOURCE')
    _, prompt = apihead.build_chat_prompt('casper', [
        {'author': 'adrian', 'kind': 'analisis', 'body': 'Fix authentication'},
        {'author': 'casper', 'kind': 'respuesta', 'body': 'Different wording'}],
        artifact='/repo/a', thread='chat-1')
    assert calls == [('Fix authentication', '/repo/a', 'chat-1')]
    assert 'MEMORY-SOURCE' in prompt


def test_memory_budget_and_missing_database(graph_db, monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    for i in range(20):
        seed(graph_db, f'decision:{i}', 'Parser', evidence=[
            {'id': i, 'author': 'casper', 'kind': 'posicion', 'body': 'x' * 10000}])
    assert len(memory_ctx.memoria_para('Parser')) <= memory_ctx.MAX_CHARS
    monkeypatch.setattr(memory_ctx, 'DB_PATH', tmp_path / 'missing.db')
    assert memory_ctx.memoria_para('Parser') == ''
    assert not memory_ctx.DB_PATH.exists()
