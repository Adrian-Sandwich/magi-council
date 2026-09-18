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
