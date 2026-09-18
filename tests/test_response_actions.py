"""Explicit UX choices override the legacy text shortcut without altering the draft."""
from unittest.mock import Mock

import pytest

import board


@pytest.mark.parametrize("action,body,kind", [
    ("resume", "Here is the missing information", "contexto"),
    ("arbitrate", "seguí is no longer the right instruction", "arbitraje"),
])
def test_explicit_split_action(action, body, kind):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [
        {"id": 4, "status": "split", "round": 1}, {"id": 9}, {"id": 4}]
    result = board.human_message(conn, "d4", body, action=action)
    assert result["kind"] == kind
    assert conn.execute.call_args_list[1].args[1] == ("d4", kind, body)


def test_stale_explicit_action_cannot_silently_add_context():
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"id": 4, "status": "open", "round": 2}
    with pytest.raises(ValueError):
        board.human_message(conn, "d4", "My final ruling", action="arbitrate")
    assert conn.execute.call_count == 1


def test_closed_follow_up_reopens_same_dossier():
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [
        {"id": 4, "thread": "d4", "status": "closed", "round": 2},
        {"id": 10},
    ]
    result = board.follow_up_decision(conn, 4, "I want to examine the practical consequences")
    assert result["decision_id"] == 4
    assert result["thread"] == "d4"
    queries = [call.args[0] for call in conn.execute.call_args_list]
    assert any("SET status = 'open'" in query for query in queries)
    update = next(query for query in queries if "SET status = 'open'" in query)
    assert "- 'approved_conditions'" in update


def test_follow_up_after_merged_production_opens_linked_decision(monkeypatch):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "id": 28, "thread": "d28", "status": "closed", "round": 7,
        "artifact": "C:/repo", "minority_report": {"execution_state": "merged"},
    }
    monkeypatch.setattr(board, "start_decision", lambda *a, **kw: {
        "decision_id": 33, "thread": "d33", "seats": ["melchior"], "degraded": [],
    })

    result = board.follow_up_decision(conn, 28, "qué sigue y qué se hizo?")

    assert result["action"] == "opened_follow_up"
    assert result["decision_id"] == 33
    assert result["source_decision_id"] == 28
    assert result["production"] is False
    assert not any("SET status = 'open'" in call.args[0]
                   for call in conn.execute.call_args_list)


@pytest.mark.parametrize("action", ["save", "stop"])
def test_next_move_can_be_recorded_without_opening_work(action):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "id": 28, "status": "closed", "minority_report": {
            "execution_state": "merged", "synthesis": {"next_move": {
                "title": "Conectar eventos", "expected_result": "Eventos activos", "risk": "Medio",
            }},
        },
    }
    result = board.continue_from_proposal(conn, 28, action)
    assert result == {"decision_id": 28, "action": f"continuation_{action}"}
    patch = conn.execute.call_args_list[1].args[1][0].obj
    assert patch["continuation"] == {"action": action, "title": "Conectar eventos"}


@pytest.mark.parametrize("action,production", [("execute", True), ("discuss", False)])
def test_next_move_opens_fresh_linked_decision(monkeypatch, action, production):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "id": 28, "status": "closed", "minority_report": {
            "execution_state": "merged", "synthesis": {"next_move": {
                "title": "Conectar eventos", "expected_result": "Eventos activos", "risk": "Medio",
            }},
        },
    }
    seen = {}
    def follow_up(_conn, identifier, body):
        seen["body"] = body
        return {"action": "opened_follow_up", "decision_id": 33,
                "source_decision_id": identifier, "production": board.is_execution_request(body)}
    monkeypatch.setattr(board, "follow_up_decision", follow_up)
    result = board.continue_from_proposal(conn, 28, action)
    assert result["decision_id"] == 33
    assert result["production"] is production
    assert result["continuation_action"] == action


@pytest.mark.parametrize("text", [
    "pues arréglalo", "vamos con tu plan", "adelante con el plan",
    "haz lo que propones", "aplica la propuesta", "ok sigamos con eso",
    "ok vmaos con los camios", "apruebo tu plan",
])
def test_execution_intent_understands_natural_followups(text):
    assert board.is_execution_request(text)


def test_execute_approved_decision_evolves_same_dossier():
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [
        {"id": 4, "thread": "d4", "status": "closed", "round": 3,
         "ruling": "conditional", "artifact": "C:/repo"},
        None,
        {"id": 11},
        None,
        None,
    ]
    result = board.execute_approved_decision(conn, 4, "vamos con tu plan")
    assert result["action"] == "execution_requested"
    assert result["decision_id"] == 4
    queries = [call.args[0] for call in conn.execute.call_args_list]
    assert any("status = 'executing'" in query and "production = true" in query for query in queries)
    assert any("EJECUCIÓN SOLICITADA" in call.args[1][1]
               for call in conn.execute.call_args_list if call.args[0].lstrip().startswith("INSERT INTO messages"))


def test_execute_approved_review_resumes_parent_with_conditions():
    conn = Mock()
    review = {
        "id": 30, "thread": "d30", "status": "closed", "round": 3,
        "ruling": "conditional", "minority_report": {
            "approved_conditions": ["move playtests", "add history test"],
        },
    }
    parent = {"id": 28, "thread": "d28", "status": "executing"}
    conn.execute.return_value.fetchone.side_effect = [review, parent, {"id": 81}]

    result = board.execute_approved_decision(conn, 30, "apruebo tu plan")

    assert result["action"] == "corrections_requested"
    assert result["decision_id"] == 28
    assert result["review_id"] == 30
    update = next(call for call in conn.execute.call_args_list
                  if "UPDATE decisions" in call.args[0])
    patch = update.args[1][0].obj
    assert patch["execution_state"] == "pending"
    assert patch["approved_conditions"] == ["move playtests", "add history test"]
    assert "hallazgos explícitamente diferidos" in patch["approved_plan"]
    assert update.args[1][1] == 28


def test_merge_by_majority_autoriza_solo_una_revision_aprobada_2_de_3():
    """La #28 quedó tres veces en MERGE PENDIENTE sin salida desde la
    pantalla. El operador puede autorizar el merge de una revisión aprobada
    por 2/3; queda como arbitraje en el journal y el relay integra con las
    comprobaciones de siempre."""
    import pytest
    import board
    rows = {
        28: {"id": 28, "thread": "d28", "status": "executing",
             "minority_report": {"execution_state": "merge_blocked", "execution": {"review_id": 31}}},
        31: {"id": 31, "thread": "d31", "status": "closed", "ruling": "yes", "confidence": 0.66},
    }
    written = []

    class _One:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Conn:
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("SELECT * FROM decisions"):
                return _One(rows.get(params[0]))
            written.append((q, params))
            return _One(None)

    result = board.merge_by_majority(Conn(), 28)
    assert result == {"decision_id": 28, "review_id": 31, "action": "merge_authorized"}
    update, insert = written
    assert update[1][0].obj == {"merge_override": {"review_id": 31, "by": "adrian"}, "execution_state": "reviewing"}
    assert insert[1][0] == "d28" and "MERGE AUTORIZADO POR MAYORÍA" in insert[1][1]

    rows[31]["confidence"] = 0.5
    with pytest.raises(ValueError, match="mayoría"):
        board.merge_by_majority(Conn(), 28)
    rows[31].update(confidence=0.66, ruling="conditional")
    with pytest.raises(ValueError, match="aprobando"):
        board.merge_by_majority(Conn(), 28)
    rows[28]["minority_report"]["execution_state"] = "reviewing"
    with pytest.raises(ValueError, match="merge pendiente"):
        board.merge_by_majority(Conn(), 28)
