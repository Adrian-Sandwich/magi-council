import json
import os
from pathlib import Path
import uuid

import pytest

import outcomes
from experience import build
import memory_ctx
from test_memory_retrieval import seed


def test_merge_is_not_a_success_report_and_user_corrections_remain():
    reports = [{'id':1,'status':'worked'}, {'id':2,'status':'failed'}]
    data = build({'outcome_reports':reports, 'system_messages':[
        {'id':3,'author':'magi','body':'MERGE OK — commit abc'},
        {'id':4,'author':'adrian','body':'MERGE OK — fake'}]})
    assert data['latest_report']['status'] == 'failed'
    assert data['revised']
    assert len(data['reports']) == 2
    assert data['events'] == [{'kind':'merged','message_id':3,'detail':'MERGE OK — commit abc','created_at':None}]
    assert build({'system_messages':[{'id':1,'author':'magi','body':'MERGE OK — abc'}]})['latest_report'] is None


def test_retrieval_includes_evidence_and_limits_of_lesson(graph_db, monkeypatch):
    monkeypatch.setattr(memory_ctx,'DB_PATH',Path(graph_db.execute('PRAGMA database_list').fetchone()[2]))
    experience = {'latest_report':{'id':7,'message_id':70,'status':'failed','observation':'La caché perdió datos',
                  'evidence':'Prueba de reinicio fallida','lesson':'Persistir antes de confirmar'},'revised':True,
                  'events':[{'kind':'merged','message_id':60,'detail':'MERGE OK — abc'}]}
    seed(graph_db,'decision:1','Caché',experience=experience)
    text = memory_ctx.memoria_para('Persistir antes de confirmar')
    assert 'outcome:7' in text and 'message:70' in text and 'Prueba de reinicio fallida' in text
    assert 'no regla universal' in text and 'corrige resultados anteriores' in text
    assert 'no demuestra utilidad' in text


def test_real_outcome_is_idempotent_and_does_not_reopen_decision():
    dsn = os.environ.get('CLAMI_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL opt-in')
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn:
        conn.execute('SET search_path TO pg_temp')
        conn.execute("CREATE TEMP TABLE decisions (id bigint PRIMARY KEY,thread text,status text)")
        conn.execute("CREATE TEMP TABLE messages (id bigserial PRIMARY KEY,thread text,author text,kind text,body text,artifact text)")
        migration = Path(__file__).resolve().parents[1] / 'debate-mcp/schema/005_outcomes.sql'
        conn.execute(migration.read_text().replace('CREATE TABLE decision_outcomes','CREATE TEMP TABLE decision_outcomes'))
        conn.execute("INSERT INTO decisions VALUES (1,'test','closed')")
        payload = {'decision_id':1,'request_id':str(uuid.uuid4()),'status':'worked','observation':'Carga correctamente',
                   'evidence':'Prueba manual en dispositivo','lesson':'Validar en ese dispositivo'}
        with conn.transaction():
            first = outcomes.record(conn,payload)
        with conn.transaction():
            assert outcomes.record(conn,payload) == first
        with pytest.raises(ValueError), conn.transaction():
            outcomes.record(conn,dict(payload,status='failed'))
        with conn.transaction():
            outcomes.record(conn,dict(payload,status='failed',request_id=str(uuid.uuid4())))
        assert conn.execute('SELECT count(*) AS n FROM decision_outcomes').fetchone()['n'] == 2
        assert conn.execute('SELECT count(*) AS n FROM messages').fetchone()['n'] == 2
        assert conn.execute('SELECT status FROM decisions').fetchone()['status'] == 'closed'


def test_calificacion_rapida_no_exige_observacion_ni_evidencia():
    """Cero outcomes en dos semanas porque el formulario pedía dos textos. La
    calificación rápida (un clic) guarda el resultado con un detalle fijo;
    sin `quick` la validación sigue igual."""
    import uuid
    inserted = []

    class Conn:
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("SELECT id,thread,status FROM decisions"):
                return _One({"id": 5, "thread": "d5", "status": "closed"})
            if q.startswith("SELECT * FROM decision_outcomes"):
                return _One(None)
            inserted.append((q, params))
            return _One({"id": 9})

    class _One:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row

    payload = {"decision_id": 5, "status": "worked", "observation": "", "evidence": "", "lesson": "",
               "request_id": str(uuid.uuid4())}
    with pytest.raises(ValueError):
        outcomes.record(Conn(), payload)
    assert outcomes.record(Conn(), dict(payload, quick=True)) == {"id": 9, "decision_id": 5}
    message, row = inserted
    assert "calificación rápida" in message[1][1] and row[1][3] == "calificación rápida, sin detalle"
