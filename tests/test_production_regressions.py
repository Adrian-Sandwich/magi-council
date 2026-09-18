"""Regression cases for execution failures and the approved plan contract."""

from contextlib import nullcontext
from unittest.mock import Mock
import subprocess

import pytest

import board
import relay


@pytest.mark.parametrize("votes", [
    ["conditional", "conditional", "conditional"],
    ["conditional", "conditional", "yes"],
    ["yes", "yes", "conditional"],
])
def test_executor_receives_conditions_from_all_current_conditional_votes(votes):
    seats = ["melchior", "balthasar", "casper"]
    d = dict(id=1, thread="d1", title="plan", status="open", round=2,
             heads=seats, protocol="vote", production=True)
    positions = [dict(head=s, round=2, position=v, conditions=[s, "tests"])
                 for s, v in zip(seats, votes)]
    positions.append(dict(head="melchior", round=1, position="conditional",
                          conditions=["obsolete"]))
    conn = Mock()
    saved = {}

    def execute(query, params=()):
        result = Mock()
        if "SELECT * FROM decisions" in query:
            result.fetchone.return_value = d
        elif "SELECT 1 FROM positions" in query:
            result.fetchone.return_value = None
        elif "INSERT INTO messages" in query:
            result.fetchone.return_value = {"id": 9}
        elif "SELECT head, round" in query:
            result.fetchall.return_value = positions
        elif "SET status = 'executing'" in query:
            saved.update(params[2].obj)
        return result

    conn.execute.side_effect = execute
    board.record_position(conn, 1, seats[-1], votes[-1], "reason")
    expected = list(dict.fromkeys(c for s, v in zip(seats, votes)
                                 if v == "conditional" for c in [s, "tests"]))
    assert relay._condiciones_aprobacion({"minority_report": saved}) == expected


def test_inline_timeout_kills_and_reaps_before_unregistering(monkeypatch, tmp_path):
    proc = Mock()
    proc.pid = 1234
    proc.wait.side_effect = [subprocess.TimeoutExpired("stub", 1), -9]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc)
    killed = []

    def kill(p):
        assert relay._procs["d1::casper"] is p
        killed.append(p)

    monkeypatch.setattr(relay, "_kill_tree", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        relay._run_cli_inline({"seat": "casper", "bin": "stub"}, "prompt",
                              str(tmp_path), 1, token="d1::casper")
    assert killed == [proc]
    assert proc.wait.call_count == 2
    assert "d1::casper" not in relay._procs


@pytest.mark.parametrize("body,retry", [("mirá los tests", False), ("seguí", True)])
def test_execution_context_only_retries_explicitly(body, retry):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = (
        [{"id": 1, "status": "executing", "round": 1, "minority_report": {}}, {"id": 9}]
    )
    board.human_message(conn, "d1", body)
    queries = [c.args[0] for c in conn.execute.call_args_list]
    assert not any("DELETE" in q for q in queries)
    updates = [c for c in conn.execute.call_args_list if "jsonb_build_object('execution_state'" in c.args[0]]
    assert bool(updates) == retry
    if retry:
        assert updates[0].args[1] == ("pending", 1)


def test_explicit_retry_after_merge_failure_retries_only_the_merge():
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [
        {"id": 1, "status": "executing", "round": 1,
         "minority_report": {"execution_state": "merge_blocked"}},
        {"id": 9},
    ]

    board.human_message(conn, "d1", "retry")

    update = next(c for c in conn.execute.call_args_list
                  if "jsonb_build_object('execution_state'" in c.args[0])
    assert update.args[1] == ("reviewing", 1)


def test_executor_spawn_error_is_persisted_for_manual_retry(monkeypatch):
    monkeypatch.setattr(relay, "executor_seat", lambda: None)
    conn = Mock()
    conn.transaction.side_effect = lambda: nullcontext()
    monkeypatch.setattr(relay, "connect", lambda: nullcontext(conn))
    monkeypatch.setattr(relay, "event", lambda *a, **kw: None)
    relay._run_executor_turn({"id": 1, "thread": "d1"}, "/unused")
    calls = conn.execute.call_args_list
    assert any(len(c.args) > 1 and c.args[1] and getattr(c.args[1][0], "obj", {}).get("execution_state") == "failed"
               for c in calls)
    assert any("EJECUCIÓN FALLIDA" in str(c.args) for c in calls)


def test_abort_durante_la_publicacion_cierra_el_evento_del_disparo(monkeypatch, tmp_path):
    """El operador aborta mientras el ejecutor sale: la publicación se
    cancela (return temprano) pero el evento trigger_spawned del disparo
    tiene que cerrarse con trigger_done rc=-1 — sin esto el JSONL de
    métricas quedaba con un disparo abierto."""
    import sys

    monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
    monkeypatch.setattr(relay.heads, "load", lambda: [
        {"seat": "ejec", "name": "x", "type": "cli", "executor": True,
         "bin": sys.executable, "args": [],
         "execution_args": ["--workspace", "{git_common_dir}"]},
    ])
    monkeypatch.setattr(relay.production, "plan",
                        lambda cwd, did, prev: {"branch": "magi/d1", "base_branch": "main",
                                                "repo": str(tmp_path)})
    monkeypatch.setattr(relay.production, "prepare", lambda run: str(tmp_path))
    monkeypatch.setattr(relay.production, "common_dir", lambda repo: "C:/git/common")
    monkeypatch.setattr(relay.production, "commit_execution", lambda run, message: "sha1")
    monkeypatch.setattr(relay.production, "review_target", lambda run: ("sha1", "diff"))

    proc = Mock()
    proc.pid = 1234
    proc.wait.return_value = 0
    spawned = {}
    def popen(*args, **kwargs):
        spawned.update(command=args[0], stdin=kwargs.get("stdin"))
        return proc
    monkeypatch.setattr(subprocess, "Popen", popen)

    conn = Mock()
    conn.transaction.side_effect = lambda: nullcontext()

    def execute(query, params=()):
        result = Mock()
        if "SELECT status FROM decisions" in query:
            # abort: al volver el ejecutor, la decisión ya no está 'executing'
            result.fetchone.return_value = {"status": "closed"}
        return result

    conn.execute.side_effect = execute
    monkeypatch.setattr(relay, "connect", lambda: nullcontext(conn))

    events = []
    monkeypatch.setattr(relay, "event", lambda kind, **kw: events.append((kind, kw)))

    relay._execute_plan({"id": 1, "thread": "d1", "title": "plan",
                         "minority_report": None}, str(tmp_path))

    done = [kw for kind, kw in events if kind == "trigger_done"]
    assert len(done) == 1, "el disparo se cerró una sola vez"
    assert done[0]["rc"] == -1 and "abortado" in done[0]["error"]
    assert spawned["command"][-1] == "-", "Codex recibe el prompt multilínea por stdin"
    assert spawned["command"][-3:-1] == ["--workspace", "C:/git/common"]
    assert spawned["stdin"] is not None
    assert not any("EJECUCIÓN FALLIDA" in str(c.args)
                   for c in conn.execute.call_args_list), "el abort no es un fallo"
