"""Failed turns never become votes, auto-retries, or errors in a later round."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import board
import magi_ui
import relay
import turn_errors
from test_production_git import pg  # isolated PostgreSQL temp tables


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(relay, 'EVENTS_PATH', tmp_path / 'events.jsonl')
    relay._failed_turns.clear()
    relay._pending_turn_errors.clear()
    relay._inflight.clear()
    relay._procs.clear()
    yield
    relay._failed_turns.clear()
    relay._pending_turn_errors.clear()
    relay._inflight.clear()
    relay._procs.clear()


def meta():
    return {'decision_id': 4, 'round': 1, 'thread': 'd4', 'author': 'melchior', 'token': 'd4::melchior'}


@pytest.mark.parametrize('code,timeout', [(0, False), (2, False), (-1, True)])
def test_mcp_completion_checks_vote_and_always_releases_slot(monkeypatch, code, timeout):
    proc = Mock()
    proc.wait.side_effect = [subprocess.TimeoutExpired('agent', 900), -1] if timeout else [code]
    errors = []
    monkeypatch.setattr(relay, '_report_turn_error', lambda m, reason: errors.append(reason) or True)
    monkeypatch.setattr(relay, '_kill_tree', lambda p: None)
    relay._inflight.add('d4::melchior')
    relay._procs['d4::melchior'] = proc
    relay._supervise(proc, meta())
    assert errors and ('Tiempo agotado' in errors[0]) == timeout
    assert 'd4::melchior' not in relay._inflight
    assert 'd4::melchior' not in relay._procs
    event = json.loads(relay.EVENTS_PATH.read_text().splitlines()[-1])
    assert event['outcome'] == 'failed' and event['timed_out'] == timeout


def test_database_outage_keeps_failure_blocked_until_recorded(monkeypatch):
    monkeypatch.setattr(relay, 'connect', Mock(side_effect=OSError('offline')))
    assert relay._report_turn_error(meta(), 'no vote')
    assert relay._failed_turns['d4::melchior'] == 1
    assert relay._pending_turn_errors['d4::melchior'][1] == 'no vote'
    conn = Mock()
    conn.__enter__ = Mock(return_value=conn)
    conn.__exit__ = Mock(return_value=False)
    conn.transaction.return_value = conn
    monkeypatch.setattr(relay, 'connect', lambda: conn)
    monkeypatch.setattr(turn_errors, 'record', lambda *a: True)
    assert relay._report_turn_error(meta(), 'no vote')
    assert not relay._pending_turn_errors


@pytest.mark.parametrize('token', ['d4::melchior', 'synthesis:melchior'])
def test_file_transport_preserves_long_unicode_task_and_external_cwd(tmp_path, allow_real_processes, token):
    stub = tmp_path / 'fake_cli.py'
    stub.write_text("import sys,re,pathlib\n"
                    "task=re.search(r'at (.+?) and follow',sys.argv[-1]).group(1)\n"
                    "text=pathlib.Path(task).read_text(encoding='utf-8')\n"
                    "assert len(text)>40000 and 'Espa\\u00f1ol' in text\n"
                    "print('POSITION: yes')\nprint(pathlib.Path.cwd())\n", encoding='utf-8')
    external = tmp_path / 'other repo'
    external.mkdir()
    seat = {'seat': 'melchior', 'bin': sys.executable, 'args': [str(stub)], 'prompt_transport': 'file'}
    answer = relay._run_cli_inline(seat, 'Español ' * 6000, str(external), 10, token=token)
    assert str(external) in answer and 'POSITION: yes' in answer
    assert list(tmp_path.glob(token.replace('::', '_').replace(':', '_') + '_*.log'))


def start(pg):
    return board.start_decision(pg, 'Analyze another repository', artifact='C:/other',
                                seats=['melchior', 'balthasar', 'casper'])['decision_id']


def test_stdin_only_transport_has_no_extra_positional_argument(tmp_path, allow_real_processes):
    stub = tmp_path / 'stdin_cli.py'
    stub.write_text("import sys\nassert len(sys.argv)==1\n"
                    "assert sys.stdin.buffer.read().decode('utf-8')=='Revisi\\u00f3n del repo'\n"
                    "print('POSITION: yes')\n", encoding='utf-8')
    seat = {'seat': 'casper', 'bin': sys.executable, 'args': [str(stub)], 'prompt_transport': 'stdin-only'}
    assert 'POSITION: yes' in relay._run_cli_inline(seat, 'Revisión del repo', str(tmp_path), 10)


def row(pg, did):
    return pg.execute('SELECT * FROM decisions WHERE id=%s', (did,)).fetchone()


def test_failed_turn_is_durable_visible_and_retry_preserves_votes(pg, monkeypatch):
    did = start(pg)
    # dos votos distintos: sin mayoría, el error deja la ronda esperando al operador
    board.record_position(pg, did, 'balthasar', 'yes', 'existing evidence')
    board.record_position(pg, did, 'casper', 'no', 'other evidence')
    with pg.transaction():
        assert turn_errors.record(pg, did, 'melchior', 1, 'Missing board tools')
        assert not turn_errors.record(pg, did, 'melchior', 1, 'duplicate')
    d = row(pg, did)
    assert d['status'] == 'open'
    assert magi_ui.verdict_badge(d)['text'] == 'TURN FAILED'
    assert pg.execute('SELECT count(*) n FROM positions').fetchone()['n'] == 2
    calls = []
    monkeypatch.setattr(relay, 'trigger', lambda *a, **k: calls.append(a) or True)
    monkeypatch.setattr(relay.memory_ctx, 'memoria_para', lambda *a, **k: None)
    state = {'threads': {}, 'spawn_failures': {}, 'last_id': 0, 'pending': []}
    # A fresh state simulates restarting the relay: the DB still blocks the turn.
    relay.fire_decision_turns(pg, state, relay.fetch_open_decisions(pg)[0])
    assert not calls
    failures = {seat: error['id'] for seat, error in turn_errors.active(d).items()}
    with pg.transaction():
        turn_errors.retry(pg, did, failures)
    assert not turn_errors.active(row(pg, did))
    assert pg.execute('SELECT count(*) n FROM positions').fetchone()['n'] == 2
    relay.fire_decision_turns(pg, state, relay.fetch_open_decisions(pg)[0])
    assert len(calls) == 1 and calls[0][0] == 'melchior'
    with pytest.raises(ValueError):
        turn_errors.retry(pg, did, failures)
    with pg.transaction():
        board.record_position(pg, did, 'melchior', 'yes', 'Recovered', expected_round=1)
    d = row(pg, did)
    assert d['status'] == 'closed' and d['ruling'] == 'yes' and not d['minority_report'].get('degraded')


def test_two_matching_votes_and_a_failed_seat_close_degraded(pg):
    """#96: casper hit its session limit after melchior and balthasar had
    both approved; the review stayed open for good. Now the recorded
    failure lets the round close degraded with majority confidence, and
    the dossier keeps who failed and why."""
    did = start(pg)
    board.record_position(pg, did, 'balthasar', 'yes', 'existing evidence')
    board.record_position(pg, did, 'casper', 'yes', 'other evidence')
    with pg.transaction():
        assert turn_errors.record(pg, did, 'melchior', 1, 'session limit')
    d = row(pg, did)
    assert d['status'] == 'closed' and d['ruling'] == 'yes' and round(float(d['confidence']), 2) == 0.66
    mr = d['minority_report']
    assert mr['degraded'] is True and mr['errored'] == ['melchior']
    assert mr['turn_errors']['melchior']['message'] == 'session limit'
    assert not turn_errors.active(d), 'cerrada: ya no hay errores activos que reintentar'
    bodies = [m['body'] for m in pg.execute('SELECT body FROM messages ORDER BY id').fetchall()]
    assert any(b.startswith('ERROR EN TURNO de melchior') for b in bodies)
    assert any('APROBADA' in b or 'yes' in b.lower() for b in bodies[-1:]), bodies[-1]
    with pytest.raises(ValueError):
        turn_errors.retry(pg, did, {'melchior': mr['turn_errors']['melchior']['id']})


def test_stale_failure_and_vote_do_not_contaminate_new_round(pg):
    did = start(pg)
    pg.execute('UPDATE decisions SET round=2 WHERE id=%s', (did,))
    assert not turn_errors.record(pg, did, 'melchior', 1, 'late failure')
    with pytest.raises(ValueError, match='ronda'):
        board.record_position(pg, did, 'melchior', 'yes', 'late vote', expected_round=1)
    assert not pg.execute('SELECT * FROM positions').fetchall()


def test_successful_mcp_vote_is_not_marked_failed(pg):
    did = start(pg)
    board.record_position(pg, did, 'melchior', 'yes', 'Registered via MCP')
    assert not turn_errors.record(pg, did, 'melchior', 1, 'process exited without vote')
    assert not turn_errors.active(row(pg, did))


def test_quarantined_seat_is_left_out_of_new_decisions(pg, monkeypatch):
    import metrics
    monkeypatch.setattr(metrics, "quarantined_seats", lambda *a, **k: {"casper": "3 turnos de voto fallidos seguidos hoy"})
    with pg.transaction():
        opened = board.start_decision(pg, 'Sin casper hoy', artifact=None, protocol='vote')
    assert opened['seats'] == ['melchior', 'balthasar'] and opened['quarantined'] == ['casper']
    d = row(pg, opened['decision_id'])
    assert d['heads'] == ['melchior', 'balthasar'] and d['minority_report']['quarantined'] == ['casper']
    bodies = [m['body'] for m in pg.execute('SELECT body FROM messages ORDER BY id').fetchall()]
    assert any(b.startswith('EN CUARENTENA: casper') for b in bodies)
    # dos votos iguales cierran: la decisión no espera a nadie más
    board.record_position(pg, opened['decision_id'], 'melchior', 'yes', 'a')
    board.record_position(pg, opened['decision_id'], 'balthasar', 'yes', 'b')
    assert row(pg, opened['decision_id'])['status'] == 'closed'


def test_cli_json_output_feeds_real_usage_into_stats(tmp_path, allow_real_processes):
    """casper en modo `claude-json`: el relay agrega el flag, saca el voto del
    campo `result` y deja tokens y costo reales en las stats del turno (lo que
    metrics.py convierte en la columna costo, sin claves API)."""
    stub = tmp_path / 'fake_claude.py'
    stub.write_text("import sys, json\n"
                    "args = sys.argv[1:]\n"
                    "assert args[-2:] == ['--output-format', 'json'], args\n"
                    "sys.stdin.read()\n"
                    "print('[stderr-ish] warning line')\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
                    "                  'result': 'POSITION: yes\\nvisto', 'total_cost_usd': 0.05,\n"
                    "                  'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 990,\n"
                    "                            'cache_read_input_tokens': 0, 'output_tokens': 7}}))\n",
                    encoding='utf-8')
    seat = {'seat': 'casper', 'type': 'cli', 'bin': sys.executable, 'args': [str(stub)],
            'prompt_transport': 'stdin-only', 'output_format': 'claude-json'}
    stats = {}
    text = relay._run_cli_inline(seat, 'prompt de prueba', str(tmp_path), 30, token='d9::casper', stats=stats)
    assert text == 'POSITION: yes\nvisto'
    assert stats == {'input_tokens': 1000, 'output_tokens': 7, 'cached_input_tokens': 0, 'cost_usd': 0.05}


def test_cli_json_is_error_with_rc_zero_fails_the_turn(tmp_path, allow_real_processes):
    stub = tmp_path / 'fake_claude.py'
    stub.write_text("import sys, json\n"
                    "sys.stdin.read()\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True,\n"
                    "                  'result': \"You've hit your session limit\"}))\n", encoding='utf-8')
    seat = {'seat': 'casper', 'type': 'cli', 'bin': sys.executable, 'args': [str(stub)],
            'prompt_transport': 'stdin-only', 'output_format': 'claude-json'}
    with pytest.raises(RuntimeError, match='session limit'):
        relay._run_cli_inline(seat, 'prompt', str(tmp_path), 30, token='d9::casper', stats={})
