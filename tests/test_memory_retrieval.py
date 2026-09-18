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


def test_memoria_con_fuentes_devuelve_los_ids_que_entraron(graph_db, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    seed(graph_db, 'decision:5', 'Authentication timeout', objective='Fix login timeout')
    text, ids = memory_ctx.memoria_con_fuentes('authentication timeout')
    assert ids == ['decision:5'] and 'decision:5' in text
    assert memory_ctx.memoria_con_fuentes('receta de pastel') == ('', [])


def test_la_memoria_no_rellena_con_nodos_del_mismo_repo_sin_tema(graph_db, monkeypatch):
    """El bloque salía siempre a 6.4–6.5k chars: un nodo del mismo repositorio
    sin ninguna palabra en común entraba por el bonus de repo. Ahora hace
    falta tema (título, dos términos del contenido o similitud) o el mismo thread."""
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    seed(graph_db, 'decision:1', 'Migrar el esquema a jsonb', artifact='C:/work/app',
         objective='Migrar el esquema a jsonb sin perder datos')
    seed(graph_db, 'decision:2', 'Cambiar el color del botón', artifact='C:/work/app',
         objective='Un botón naranja en la barra')
    seed(graph_db, 'decision:3', 'Sonido al enviar', artifact='C:/work/app',
         evidence=[{'id': 9, 'author': 'casper', 'kind': 'posicion', 'body': 'el esquema jsonb migrado ayer'}])
    hits = memory_ctx.retrieve('¿migramos el esquema jsonb?', artifact='C:/work/app')
    assert [h['id'] for h in hits] == ['decision:1', 'decision:3']   # #2: mismo repo, sin tema → fuera
    assert memory_ctx.retrieve('rediseñar la pantalla de inicio', artifact='C:/work/app') == []


def test_repo_brief_resume_el_grafo_de_codigo_sin_tests_ni_builtins(graph_db, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    monkeypatch.setattr(memory_ctx, '_code_project_for', lambda artifact: 'proj' if artifact == 'C:/work/app' else None)

    def code(qn, tag, path, **props):
        db.upsert_node(graph_db, id=f'code:proj:{qn}', domain='code', source='t', updated_at='2026',
                       label=qn.rsplit('.', 1)[-1], tag=tag, props=dict(project='proj', file_path=path, **props))
    code('app.py.__file__', 'File', 'app.py', last_modified=200)
    code('core.py.__file__', 'File', 'core.py', last_modified=100)
    code('tests/test_core.py.__file__', 'File', 'tests/test_core.py', last_modified=300)
    code('core.parse', 'Function', 'core.py'); code('core.run', 'Function', 'core.py')
    code('app.main', 'Function', 'app.py'); code('app.index', 'Route', 'app.py')
    code('fake.route', 'Route', ''); code('builtins.len', 'Function', '<python-builtins>')
    code('tests.test_core.test_parse', 'Function', 'tests/test_core.py')
    code('src', 'Folder', 'src')
    for src, dst in (('app.main', 'core.parse'), ('app.main', 'core.run'), ('tests.test_core.test_parse', 'core.parse'),
                     ('app.main', 'builtins.len')):
        db.upsert_edge(graph_db, f'code:proj:{src}', f'code:proj:{dst}', 'CALLS', source='t', updated_at='2026')
    graph_db.execute("CREATE TABLE IF NOT EXISTS ingest_runs (source TEXT PRIMARY KEY, finished_at REAL NOT NULL, summary TEXT)")
    graph_db.commit()

    brief = memory_ctx.repo_brief('C:/work/app')
    assert brief.startswith('Mapa del repositorio')
    assert '3 archivos (1 de tests)' in brief and '4 funciones' in brief
    assert 'core.py (2 · 3)' in brief and 'app.py (1 · 0)' in brief
    assert 'tests/test_core.py' not in brief.split('Cambiados')[0].split('Archivos')[1], 'los tests no entran al ranking'
    assert 'python-builtins' not in brief and 'fake.route' not in brief and 'index (app.py)' in brief
    assert 'Carpetas: src' in brief
    assert brief.split('Cambiados más recientemente: ')[1].startswith('tests/test_core.py, app.py')
    assert memory_ctx.repo_brief('C:/otro') == ''


def test_el_bloque_no_repite_el_journal_del_mismo_thread_ni_deja_que_un_dossier_lo_llene(graph_db, monkeypatch):
    """El bloque salía siempre a 6.5k: la decisión en curso recibía su propio
    journal como 'memoria' (ya va en el prompt) y un dossier con cinco
    posiciones de 1800 chars llenaba todo. Ahora: nada del mismo thread; de
    otros dossiers, humano + resultado + una cabeza, 300 chars cada uno."""
    from pathlib import Path
    monkeypatch.setattr(memory_ctx, 'DB_PATH', Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    heads_ = [{'id': i, 'author': a, 'kind': 'posicion', 'body': f'POSICION-{a} ' + 'x' * 1700}
              for i, a in enumerate(('melchior', 'balthasar', 'casper'), start=10)]
    human = [{'id': 1, 'author': 'adrian', 'kind': 'contexto', 'body': 'CONTEXTO-HUMANO migrar jsonb'}]
    result = [{'id': 20, 'author': 'magi', 'kind': 'resultado', 'body': 'RESULTADO cerrada yes'}]
    seed(graph_db, 'decision:9', 'Migrar jsonb', thread='d9', evidence=human + heads_ + result)
    seed(graph_db, 'decision:8', 'Migrar jsonb antes', evidence=human + heads_ + result)
    mem = memory_ctx.memoria_para('migrar jsonb', thread='d9')
    own, other = mem.split('[decision:8')
    assert 'POSICION-' not in own and 'CONTEXTO-HUMANO' not in own, 'del mismo thread no se repite evidencia'
    assert 'CONTEXTO-HUMANO' in other and 'RESULTADO cerrada' in other
    assert other.count('POSICION-') == 1, 'una sola posición de cabeza por dossier ajeno'
    assert len('[decision:8' + other) <= memory_ctx.HIT_MAX_CHARS + 2
