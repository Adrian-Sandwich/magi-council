"""Evaluation cases for semantic agreement, including real transactional flow."""
import pytest
import board
import decision
import council_synthesis as synthesis
from test_production_git import pg  # isolated temporary PostgreSQL tables

SEATS = ['melchior','balthasar','casper']


def reviews(accept=True):
    return [{'seat':s,'approve':True,'accept_answer':accept,'feedback':'' if accept else 'Falta distinguir causa y sentido'} for s in SEATS]


@pytest.mark.parametrize('round_,reports,expected', [
    (2,reviews(True),'consensus'),
    (2,reviews(False),'next_round'),
    (3,reviews(False),'budget_exhausted'),
    (2,[dict(r,accept_answer=None) for r in reviews()],'unavailable'),
    (2,[dict(r,error='TimeoutExpired') for r in reviews()],'unavailable'),
    (2,reviews()[:2],'unavailable'),
    (2,[dict(r,approve=False) for r in reviews()],'next_round'),
])
def test_content_agreement_is_independent_from_editorial_approval(round_,reports,expected):
    assert decision.content_resolution(round_,SEATS,reports) == expected


def test_followup_gets_a_fresh_bounded_budget():
    assert decision.content_resolution(5,SEATS,reviews(False),round_start=4) == 'next_round'
    assert decision.content_resolution(6,SEATS,reviews(False),round_start=4) == 'budget_exhausted'
    d={'status':'open','round':4,'heads':SEATS,'protocol':'adaptive','minority_report':{'round_budget_start':4}}
    positions=[{'head':s,'round':4,'position':'info'} for s in SEATS]
    # sin ronda de contraste forzada: el seguimiento va directo a evaluar la
    # respuesta común; una objeción real abre la ronda siguiente
    assert decision.advance(d,positions)['action'] == 'assess_content'


def cast_info_round(pg,identifier):
    for seat in SEATS:
        with pg.transaction():
            act,_ = board.record_position(pg,identifier,seat,'info','Un aporte conceptual')
    return act


@pytest.mark.parametrize('accept,terminal', [(True,'consensus'),(False,'budget_exhausted')])
def test_info_waits_for_common_answer_and_objections_feed_next_round(pg,accept,terminal):
    with pg.transaction():
        opened=board.start_decision(pg,'¿Qué es ser humano?',protocol='adaptive')
    identifier=opened['decision_id']
    assert cast_info_round(pg,identifier)['action'] == 'next_round'
    assert cast_info_round(pg,identifier)['action'] == 'assess_content'
    d,version=synthesis.snapshot(pg,identifier)
    assert d['status'] == 'open' and d['round'] == 2
    result={'status':'reviewed','answer':'Una respuesta común con límites explícitos.',
            'agreements':[],'differences':[],'open_questions':[],'cycle':1,'reviews':reviews(accept)}
    assert synthesis.finish_content(pg,identifier,version,result)
    if not accept:
        d,_=synthesis.snapshot(pg,identifier)
        assert d['status']=='open' and d['round']==3
        assert pg.execute("SELECT count(*) AS n FROM messages WHERE body LIKE 'Objeción de%%'").fetchone()['n']==3
        assert cast_info_round(pg,identifier)['action']=='assess_content'
        _,version=synthesis.snapshot(pg,identifier)
        assert synthesis.finish_content(pg,identifier,version,result)
    d,_=synthesis.snapshot(pg,identifier)
    assert d['status']=='closed' and d['ruling']=='info'
    assert d['minority_report']['content_check']['state']==terminal
    assert d['minority_report']['synthesis']['content_consensus'] is accept
    assert pg.execute("SELECT count(*) AS n FROM messages WHERE kind='consulta'").fetchone()['n']==0


def test_human_context_arriving_during_review_prevents_stale_closure(pg):
    with pg.transaction():
        identifier=board.start_decision(pg,'Pregunta',protocol='adaptive')['decision_id']
    cast_info_round(pg,identifier)
    cast_info_round(pg,identifier)
    d,version=synthesis.snapshot(pg,identifier)
    with pg.transaction():
        board.human_message(pg,d['thread'],'Me refería a otra condición importante')
    assert not synthesis.finish_content(pg,identifier,version,{'reviews':reviews(True)})
    d,_=synthesis.snapshot(pg,identifier)
    assert d['status']=='open'


def test_reviewed_synthesis_replaces_raw_conditions_with_consolidated_work(pg):
    with pg.transaction():
        identifier = board.start_decision(pg, 'Mejorar repo', protocol='adaptive')['decision_id']
    _, version = synthesis.snapshot(pg, identifier)
    result = {
        'status': 'reviewed', 'answer': 'Plan acotado.', 'agreements': [],
        'differences': [], 'open_questions': [], 'reviews': reviews(True),
        'blocking_conditions': ['pytest pasa en Windows', 'CI incluye Windows'],
        'deferred_items': ['refactor grande posterior'],
    }
    assert synthesis.save(pg, identifier, version, result)
    d, _ = synthesis.snapshot(pg, identifier)
    assert d['minority_report']['approved_conditions'] == [
        'pytest pasa en Windows', 'CI incluye Windows']
    assert d['minority_report']['deferred_items'] == ['refactor grande posterior']
