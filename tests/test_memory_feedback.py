"""Calificación humana de la memoria: validación, foto de las fuentes y
rastro en el journal. Postgres de mentira; nada toca la base real."""

import pytest

import memory_feedback


class _Conn:
    def __init__(self, decision):
        self.decision = decision
        self.inserted = []

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT id, thread, round, minority_report FROM decisions"):
            rows = [self.decision] if self.decision and self.decision["id"] == params[0] else []
        elif q.startswith("INSERT INTO messages"):
            self.inserted.append(("message", params))
            rows = [{"id": 501}]
        elif q.startswith("INSERT INTO memory_feedback"):
            self.inserted.append(("feedback", params))
            rows = [{"id": 9}]
        else:
            raise AssertionError(q)
        return _Rows(rows)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


DECISION = {"id": 12, "thread": "d12", "round": 2,
            "minority_report": {"memory_sources": {"round": 1, "ids": ["decision:7", "doc:readme"]}}}


def test_record_guarda_la_foto_de_fuentes_y_deja_rastro():
    conn = _Conn(DECISION)
    result = memory_feedback.record(conn, {"decision_id": 12, "useful": False, "note": " no venía al caso "})
    assert result == {"id": 9, "decision_id": 12, "useful": False}
    kinds = [k for k, _ in conn.inserted]
    assert kinds == ["message", "feedback"]
    thread, body = conn.inserted[0][1]
    assert thread == "d12" and "no sirvió" in body and "decision:7" in body and "no venía al caso" in body
    decision_id, round_, useful, note, sources, message_id = conn.inserted[1][1]
    assert (decision_id, round_, useful, note, message_id) == (12, 1, False, "no venía al caso", 501)
    assert sources.obj == ["decision:7", "doc:readme"]


@pytest.mark.parametrize("payload, error", [
    ({"decision_id": "12", "useful": True}, "Seleccioná"),
    ({"decision_id": 12, "useful": "yes"}, "sirvió"),
    ({"decision_id": 12, "useful": True, "note": "x" * 501}, "máximo"),
    ({"decision_id": 99, "useful": True}, "no existe"),
    ("nada", "inválido"),
])
def test_record_rechaza_payloads_invalidos(payload, error):
    with pytest.raises(ValueError, match=error):
        memory_feedback.record(_Conn(DECISION), payload)


def test_record_exige_que_la_decision_haya_consultado_memoria():
    conn = _Conn({"id": 12, "thread": "d12", "round": 1, "minority_report": {}})
    with pytest.raises(ValueError, match="no consultó memoria"):
        memory_feedback.record(conn, {"decision_id": 12, "useful": True})
    assert conn.inserted == []
