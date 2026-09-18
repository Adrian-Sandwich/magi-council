import json
import subprocess

import db
import healthcheck
import ingest_debate
from explicit_memory import extract
from memory_sync import MemorySync


def test_explicit_memory_versions_and_cancellation():
    result = extract([
        {'id': 1, 'author': 'adrian', 'body': 'Objetivo: Publicar\nRestricción [datos]: Conservar datos'},
        {'id': 2, 'author': 'casper', 'body': 'Objetivo: Borrar todo'},
        {'id': 3, 'author': 'adrian', 'body': 'Objetivo: Probar primero\nPendiente [tests]: Correr pruebas'},
        {'id': 4, 'author': 'adrian', 'body': 'Pendiente [tests]: resuelto\n```\nObjetivo: ejemplo\n```\n> Objetivo: citado'},
    ])
    current = {m['kind']: m for m in result['current']}
    assert current['objetivo']['text'] == 'Probar primero'
    assert current['objetivo']['version'] == 2
    assert current['restriccion']['message_id'] == 1
    assert not current['pendiente']['active']
    assert len(result['history']) == 5


def test_checkpoint_skips_unchanged_but_recovers_missing_node(graph_db):
    graph_db.execute('CREATE TABLE debate_checkpoints (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
    row = {'body': 'primero'}
    assert ingest_debate.changed(graph_db, 'decision:1', row)
    db.upsert_node(graph_db, id='decision:1', domain='decision', source='test', updated_at='2026')
    assert not ingest_debate.changed(graph_db, 'decision:1', row)
    assert ingest_debate.changed(graph_db, 'decision:1', {'body': 'corregido'})
    graph_db.execute("DELETE FROM nodes WHERE id='decision:1'")
    assert ingest_debate.changed(graph_db, 'decision:1', {'body': 'corregido'})


def test_sync_recovers_after_timeout_without_exposing_stderr(monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired('ingestor', 60, stderr=b'private data')
    monkeypatch.setattr(subprocess, 'run', run)
    sync = MemorySync()
    sync.run_once()
    assert sync.snapshot()['status'] == 'error'
    assert 'private' not in json.dumps(sync.snapshot())
    sync.run_once()
    assert sync.snapshot()['status'] == 'ok'
    assert sync.snapshot()['last_success']
    assert sync.snapshot()['failures'] == 0
    assert calls[0]['timeout'] == 60


def test_health_reports_failed_sync_even_if_database_is_recent(tmp_path, monkeypatch):
    graph = tmp_path / 'memory.db'
    graph.touch()
    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(json.dumps({'memory_sync': {'status': 'error', 'error': 'TimeoutExpired'}}))
    monkeypatch.setattr(healthcheck, 'MEMORY_DB', graph)
    monkeypatch.setattr(healthcheck, 'HEARTBEAT_PATH', heartbeat)
    assert healthcheck.check_graph()[0] == healthcheck.WARN


def test_stale_ids_reconsulta_solo_marcas_cambiadas_o_nodos_ausentes(graph_db):
    """La sync corre cada 30s con timeout de 60s: re-agregar todos los
    mensajes de todos los threads cada vez era una bomba de tiempo. Las
    marcas baratas (último message id, md5 de la fila) deciden qué
    reconsultar; un nodo borrado del grafo se reconsulta aunque su marca
    no haya cambiado."""
    marks = {'debate_thread:d1': '10', 'decision:1': 'abc:10', 'debate_thread:d2': '4'}
    assert ingest_debate.stale_ids(graph_db, marks) == set(marks)  # primera corrida
    graph_db.executemany('INSERT OR REPLACE INTO debate_marks VALUES (?, ?)', marks.items())
    for identifier in marks:
        db.upsert_node(graph_db, id=identifier, domain=identifier.split(':')[0], source='t', updated_at='2026')
    assert ingest_debate.stale_ids(graph_db, marks) == set()
    assert ingest_debate.stale_ids(graph_db, dict(marks, **{'debate_thread:d2': '5'})) == {'debate_thread:d2'}
    graph_db.execute("DELETE FROM nodes WHERE id='decision:1'")
    assert ingest_debate.stale_ids(graph_db, marks) == {'decision:1'}


class _FakePg:
    """Postgres de mentira para main(): responde las consultas livianas con
    el universo completo y anota qué ids pidieron las pesadas."""

    def __init__(self, last_ids, digests):
        self.last_ids = last_ids      # {thread: último message id}
        self.digests = digests        # {decision id: (thread, md5)}
        self.heavy = []               # [(kind, ids)]

    def execute(self, query, params=()):
        q = ' '.join(query.split())
        if q.startswith('SELECT thread, max(id)'):
            rows = [{'thread': t, 'last_id': i} for t, i in self.last_ids.items()]
        elif q.startswith('SELECT d.id, d.thread, md5'):
            rows = [{'id': i, 'thread': t, 'digest': d} for i, (t, d) in self.digests.items()]
        elif q.startswith('WITH kc AS'):
            self.heavy.append(('threads', list(params[0])))
            rows = [{'thread': t, 'outcome_reports': [], 'artifact': None, 'n_messages': 1,
                     'first_at': _T, 'last_at': _T, 'authors': ['adrian'], 'human_messages': [],
                     'kind_counts': {'analisis': 1}} for t in params[0]]
        elif q.startswith('SELECT d.*'):
            self.heavy.append(('decisions', list(params[0])))
            rows = [{'id': i, 'thread': self.digests[i][0], 'title': f'#{i}', 'artifact': None,
                     'protocol': 'vote', 'status': 'closed', 'ruling': 'yes', 'confidence': 1.0,
                     'round': 1, 'created_by': 'adrian', 'minority_report': {}, 'created_at': _T,
                     'closed_at': _T, 'outcome_reports': [], 'system_messages': [],
                     'human_messages': [], 'last_message_at': _T, 'evidence': []} for i in params[0]]
        else:
            raise AssertionError(q)
        return _Rows(rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


from datetime import datetime, timezone  # noqa: E402
_T = datetime(2026, 9, 17, tzinfo=timezone.utc)


def test_main_incremental_reconsulta_lo_cambiado_y_barre_lo_borrado(graph_db, monkeypatch, capsys):
    pg = _FakePg({'d1': 10, 'd2': 4}, {1: ('d1', 'aaa'), 2: ('d2', 'bbb')})
    monkeypatch.setattr(ingest_debate.psycopg, 'connect', lambda *a, **kw: pg)
    class _NoClose:
        # main() cierra su conexión; el fixture necesita seguir leyendo la base
        def __getattr__(self, name):
            return getattr(graph_db, name)

        def close(self):
            pass
    monkeypatch.setattr(ingest_debate.db, 'connect', lambda: _NoClose())

    ingest_debate.main()
    assert pg.heavy == [('threads', ['d1', 'd2']), ('decisions', [1, 2])]
    assert graph_db.execute("SELECT count(*) FROM nodes WHERE domain IN ('debate_thread','decision')").fetchone()[0] == 4

    # sin cambios: ninguna consulta pesada
    pg.heavy.clear()
    ingest_debate.main()
    assert pg.heavy == []
    assert '0 reconsultados' in capsys.readouterr().out

    # un mensaje nuevo en d2 (afecta al thread y a su decisión), la fila de
    # la decisión 1 cambió (síntesis), y d1 ya no... sigue; nada borrado
    pg.last_ids['d2'] = 5
    pg.digests[1] = ('d1', 'aaa2')
    pg.heavy.clear()
    ingest_debate.main()
    assert pg.heavy == [('threads', ['d2']), ('decisions', [1, 2])]

    # una decisión desaparece del tablero: se barre aunque no se reconsulte nada más
    del pg.digests[2]
    pg.heavy.clear()
    ingest_debate.main()
    assert pg.heavy == []
    assert graph_db.execute("SELECT count(*) FROM nodes WHERE id='decision:2'").fetchone()[0] == 0
    assert graph_db.execute("SELECT count(*) FROM debate_marks WHERE id='decision:2'").fetchone()[0] == 0
