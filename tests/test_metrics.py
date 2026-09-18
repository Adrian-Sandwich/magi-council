"""metrics.py resume trigger_events.jsonl: latencia y fallos por asiento y
turno, tiempo de pared por ronda, disparos que no arrancaron. No toca
Postgres ni el log real."""

import json
from datetime import datetime, timedelta, timezone

import metrics


def _write(path, records):
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _ts(base, secs):
    return (base + timedelta(seconds=secs)).isoformat()


def test_resume_por_asiento_ronda_y_fallos_de_arranque(tmp_path):
    base = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    events = tmp_path / "trigger_events.jsonl"
    _write(events, [
        {"ts": _ts(base, 0), "event": "trigger_spawned", "author": "melchior", "turn": "answer",
         "decision_id": 7, "round": 1, "token": "d7::melchior"},
        {"ts": _ts(base, 0), "event": "trigger_spawned", "author": "casper", "turn": "cli-inline",
         "decision_id": 7, "round": 1, "token": "d7::casper"},
        {"ts": _ts(base, 40), "event": "trigger_done", "author": "casper", "turn": "cli-inline",
         "decision_id": 7, "round": 1, "rc": 0, "timed_out": False, "duration_s": 40.0},
        {"ts": _ts(base, 150), "event": "trigger_done", "author": "melchior", "turn": "answer",
         "decision_id": 7, "round": 1, "rc": 0, "timed_out": False, "duration_s": 150.0},
        {"ts": _ts(base, 200), "event": "trigger_done", "author": "melchior", "turn": "answer",
         "decision_id": 8, "round": 1, "rc": 1, "timed_out": True, "duration_s": 900.0},
        {"ts": _ts(base, 201), "event": "trigger_done", "author": "melchior", "turn": "answer",
         "decision_id": 9, "round": 1, "rc": 2, "timed_out": False, "duration_s": 10.0},
        {"ts": _ts(base, 300), "event": "trigger_spawn_failed", "error": "asiento 'casper' sin binario"},
        {"ts": _ts(base, 301), "event": "trigger_spawn_failed", "error": "asiento 'casper' sin binario"},
        "esto no es json\n",
    ])
    # una línea rota a mano, como deja una escritura cortada
    with events.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-09-17T12:10:00+00:00", "event": "trigger_done", "author": "x"')

    summary = metrics.summarize(metrics.read_events(events))

    melchior = next(s for s in summary["seats"] if s["seat"] == "melchior")
    assert melchior["n"] == 3
    assert melchior["p50_s"] == 150.0 and melchior["max_s"] == 900.0
    assert melchior["errors"] == 2 and melchior["timeouts"] == 1
    casper = next(s for s in summary["seats"] if s["seat"] == "casper")
    expected = {"seat": "casper", "turn": "cli-inline", "n": 1, "p50_s": 40.0, "p95_s": 40.0,
                "max_s": 40.0, "errors": 0, "timeouts": 0, "error_rate": 0.0}
    assert {k: casper[k] for k in expected} == expected
    assert casper["in_tokens_p50"] is None, "sin prompt_chars en el evento no se inventa un conteo"
    # la ronda de #7 va del primer disparo al último cierre: 150s de pared
    assert summary["rounds"] == {"n": 1, "p50_s": 150.0, "p95_s": 150.0, "max_s": 150.0}
    assert summary["spawn_failures"] == {"asiento 'casper' sin binario": 2}


def test_ventana_de_dias_y_generacion_rotada(tmp_path):
    now = datetime.now(timezone.utc)
    current = tmp_path / "trigger_events.jsonl"
    rotated = tmp_path / "trigger_events.jsonl.1"
    _write(rotated, [
        {"ts": _ts(now, -3 * 86400), "event": "trigger_done", "author": "casper", "turn": "api",
         "rc": 0, "duration_s": 5.0},
        {"ts": _ts(now, -40 * 86400), "event": "trigger_done", "author": "casper", "turn": "api",
         "rc": 0, "duration_s": 999.0},
    ])
    _write(current, [
        {"ts": _ts(now, -60), "event": "trigger_done", "author": "casper", "turn": "api",
         "rc": 0, "duration_s": 7.0},
    ])
    events = metrics.read_events(current, since=now - timedelta(days=7))
    assert [e["duration_s"] for e in events] == [5.0, 7.0]


def test_render_y_salida_json(tmp_path, capsys):
    events = tmp_path / "trigger_events.jsonl"
    _write(events, [{"ts": datetime.now(timezone.utc).isoformat(), "event": "trigger_done",
                     "author": "balthasar", "turn": "execute", "rc": 0, "duration_s": 12.0}])
    assert metrics.main(["--events", str(events)]) == 0
    out = capsys.readouterr().out
    assert "balthasar" in out and "execute" in out and "12s" in out
    assert metrics.main(["--events", str(events), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["seats"][0]["seat"] == "balthasar" and data["days"] == 7


def test_sin_eventos_no_revienta(tmp_path, capsys):
    assert metrics.main(["--events", str(tmp_path / "nada.jsonl")]) == 0
    assert "sin turnos" in capsys.readouterr().out


def test_tokens_aproximados_por_asiento_y_totales(tmp_path, capsys):
    events = tmp_path / "trigger_events.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write(events, [
        {"ts": now, "event": "trigger_done", "author": "casper", "turn": "cli-inline", "rc": 0, "duration_s": 30.0,
         "prompt_chars": 16000, "output_chars": 8000, "memory_chars": 6400},
        {"ts": now, "event": "trigger_done", "author": "casper", "turn": "cli-inline", "rc": 0, "duration_s": 30.0,
         "prompt_chars": 8000, "output_chars": 4000, "memory_chars": 0},
        {"ts": now, "event": "trigger_done", "author": "melchior", "turn": "answer", "rc": 0, "duration_s": 90.0},
    ])
    summary = metrics.summarize(metrics.read_events(events))
    casper = next(s for s in summary["seats"] if s["seat"] == "casper")
    assert casper["in_tokens_p50"] == 3000 and casper["out_tokens_p50"] == 1500 and casper["memory_tokens_p50"] == 800
    assert casper["in_tokens_total"] == 6000 and casper["in_measured"] == 2
    melchior = next(s for s in summary["seats"] if s["seat"] == "melchior")
    assert melchior["in_tokens_p50"] is None and melchior["in_measured"] == 0
    assert metrics.main(["--events", str(events)]) == 0
    out = capsys.readouterr().out
    assert "6,000 de entrada / 3,000 de salida" in out and "in≈tok" in out
