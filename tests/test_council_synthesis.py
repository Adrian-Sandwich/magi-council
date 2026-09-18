import json
import threading

import pytest

import council_synthesis as synthesis


BUNDLE = {'question': '¿Qué hacemos?', 'heads': ['melchior','balthasar','casper'], 'contributions': []}
SEATS = [{'seat': s, 'type': 'api'} for s in BUNDLE['heads']]
DRAFT = {'answer': 'Probar primero, conservando los datos.', 'agreements': ['Conservar datos'],
         'differences': ['El plazo sigue en discusión'], 'open_questions': []}


def test_parser_accepts_one_bounded_next_move_or_none():
    move = {'title': 'Conectar eventos', 'reason': 'Las funciones existen sin uso',
            'expected_result': 'Eventos visibles durante la partida', 'scope': 'Cuatro archivos',
            'risk': 'Medio', 'recommendation': 'discuss'}
    assert synthesis.parse(json.dumps(dict(DRAFT, next_move=move)))['next_move'] == move
    assert synthesis.parse(json.dumps(DRAFT))['next_move'] is None


def test_parser_rejects_unbounded_next_move():
    move = {'title': 'Conectar eventos', 'reason': 'Motivo', 'expected_result': 'Resultado',
            'scope': 'Alcance', 'risk': 'Riesgo', 'recommendation': 'run-forever'}
    with pytest.raises(ValueError):
        synthesis.parse(json.dumps(dict(DRAFT, next_move=move)))


def test_parser_bounds_verbose_but_valid_editor_lists():
    verbose = dict(DRAFT, agreements=[f"punto {i}" for i in range(6)])
    parsed = synthesis.parse(json.dumps(verbose))
    assert parsed["agreements"] == verbose["agreements"][:5]


def test_three_reviews_required_and_disagreement_preserved():
    calls = []
    def invoke(seat, prompt):
        calls.append(seat['seat'])
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'reviewed'
    assert len(result['reviews']) == 3
    assert result['differences'] == DRAFT['differences']
    assert len(calls) == 4


def test_writer_priority_and_reviews_run_in_parallel():
    barrier = threading.Barrier(3)
    seats = [dict(seat=s, type='api', synthesis_priority=priority)
             for s, priority in zip(BUNDLE['heads'], (30, 20, 10))]
    calls = []
    lock = threading.Lock()

    def invoke(seat, prompt):
        with lock:
            calls.append(seat['seat'])
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        barrier.wait(timeout=2)
        return json.dumps({'approve': True, 'accept_answer': True, 'feedback': ''})

    result = synthesis.compose(BUNDLE, seats, invoke)
    assert calls[0] == 'casper'
    assert result['status'] == 'reviewed'


def test_dissent_never_becomes_consensus_and_budget_is_bounded():
    calls = []
    def invoke(seat, prompt):
        calls.append(prompt)
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': seat['seat'] != 'casper', 'feedback': 'Falta el costo'})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert result['cycle'] == 2
    assert len(calls) == 8
    assert 'Falta el costo' in calls[4]


def test_missing_reviewer_is_not_approval():
    def invoke(seat, prompt):
        return json.dumps(DRAFT if 'Redactá una respuesta' in prompt else {'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS[:2], invoke)
    assert result['status'] == 'partial'
    assert result['reviews'][-1]['approve'] is False


def test_stale_result_is_not_published():
    class Transaction:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Conn:
        def transaction(self): return Transaction()
        def execute(self, sql, params=()):
            assert not sql.startswith('UPDATE')
            self.sql = sql
            return self
        def fetchone(self):
            if 'SELECT *' in self.sql:
                return {'round': 2, 'status': 'open', 'thread': 'd1'}
            return {'id': 99}
    assert not synthesis.save(Conn(), 1, {'round':1,'status':'closed','message_id':42}, DRAFT)


def test_broken_reviewer_output_is_not_approval():
    calls = []
    def invoke(seat, prompt):
        calls.append(prompt)
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return 'Provider error: invalid model'
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert not any(review['approve'] for review in result['reviews'])
    assert len(calls) == 4
    assert result['cycle'] == 1
    assert result['stop_reason'] == 'review_unavailable'


def test_draft_is_published_before_slow_review_and_survives_failed_revision():
    updates = []
    drafts = 0
    def invoke(seat,prompt):
        nonlocal drafts
        if 'Redactá una respuesta' in prompt:
            drafts += 1
            if drafts == 2:
                raise TimeoutError()
            return json.dumps(DRAFT)
        assert updates[-1]['answer'] == DRAFT['answer']
        assert updates[-1]['current_head'] == 'all heads'
        return json.dumps({'approve':False,'feedback':'Aclarar el alcance'})
    result = synthesis.compose(BUNDLE,SEATS,invoke,progress=updates.append)
    assert result['answer'] == DRAFT['answer']
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'revision_failed'
