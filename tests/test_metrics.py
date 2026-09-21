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


def test_quality_summary_cubre_resultados_memoria_tiempo_y_tokens(capsys):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    decisions = [
        {"id": 1, "status": "closed", "ruling": "yes", "production": True, "execution_state": "merged",
         "created_at": t0, "closed_at": t0 + timedelta(minutes=10), "memory_sources": {"ids": ["decision:0"]}},
        {"id": 2, "status": "closed", "ruling": "info", "production": False, "execution_state": None,
         "created_at": t0, "closed_at": t0 + timedelta(minutes=4), "memory_sources": {"ids": ["decision:1"]}},
        {"id": 3, "status": "open", "ruling": None, "production": False, "execution_state": None,
         "created_at": t0, "closed_at": None, "memory_sources": None},
    ]
    outcomes = [{"id": 1, "decision_id": 1, "status": "failed"}, {"id": 2, "decision_id": 1, "status": "worked"}]
    feedback = [{"id": 1, "decision_id": 2, "useful": True}, {"id": 2, "decision_id": 99, "useful": False}]
    events = [
        {"event": "trigger_done", "decision_id": 1, "rc": 0, "prompt_chars": 8000, "output_chars": 4000},
        {"event": "trigger_done", "decision_id": 1, "rc": 1, "prompt_chars": 8000, "output_chars": 0},
        {"event": "trigger_done", "decision_id": 2, "rc": 0, "prompt_chars": 4000, "output_chars": 2000},
        {"event": "trigger_done", "decision_id": None, "rc": 0, "prompt_chars": 4000},
    ]
    q = metrics.quality_summary(decisions, outcomes, feedback, events)
    assert q["closed"] == 2 and q["by_ruling"] == {"yes": 1, "info": 1}
    assert q["production"] == 1 and q["by_execution_state"] == {"merged": 1}
    assert q["outcomes"] == {"reported": 1, "coverage": 0.5, "by_status": {"worked": 1}}, "vale el último reporte"
    assert q["memory"] == {"with_sources": 2, "labeled": 1, "coverage": 0.5, "useful_rate": 1.0}
    # #1: (8000+8000)/4 = 4000, #2: 4000/4 = 1000 → mediana 2500; salida 1000 y 500 → 750
    assert q["wall_p50_s"] == 420 and q["tokens_in_p50"] == 2500 and q["tokens_out_p50"] == 750
    assert q["head_failures"] == 1
    text = metrics.render_quality(q, 7)
    assert "1/2 (50%)" in text and "útil en 100%" in text and "420s" in text


def test_quality_notify_manda_el_resumen_al_sistema(monkeypatch, tmp_path, capsys):
    import healthcheck
    sent = []
    monkeypatch.setattr(metrics, "_load_quality", lambda days: ([], [], []))
    monkeypatch.setattr(healthcheck, "notify", lambda title, body: sent.append((title, body)))
    assert metrics.main(["--quality", "--notify", "--events", str(tmp_path / "nada.jsonl")]) == 0
    assert "resultados reportados" in capsys.readouterr().out
    assert sent and "calidad" in sent[0][0] and "resultados reportados" in sent[0][1]


def test_uso_real_y_costo_por_asiento_api(tmp_path, capsys):
    events = tmp_path / "trigger_events.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write(events, [
        {"ts": now, "event": "trigger_done", "author": "casper", "turn": "api", "rc": 0, "duration_s": 12.0,
         "prompt_chars": 8000, "output_chars": 2000, "input_tokens": 2100, "output_tokens": 500,
         "cost_usd": 0.0315, "attempts": 3},
        {"ts": now, "event": "trigger_done", "author": "casper", "turn": "api", "rc": 0, "duration_s": 9.0,
         "prompt_chars": 4000, "output_chars": 1000, "input_tokens": 1000, "output_tokens": 200,
         "cost_usd": 0.015, "attempts": 1},
        {"ts": now, "event": "trigger_done", "author": "melchior", "turn": "cli-inline", "rc": 0, "duration_s": 90.0},
    ])
    summary = metrics.summarize(metrics.read_events(events))
    casper = next(s for s in summary["seats"] if s["seat"] == "casper")
    assert casper["usage_in_total"] == 3100 and casper["usage_out_total"] == 700 and casper["usage_measured"] == 2
    assert casper["cost_usd_total"] == 0.0465 and casper["retries"] == 2
    melchior = next(s for s in summary["seats"] if s["seat"] == "melchior")
    assert melchior["cost_usd_total"] is None and melchior["usage_measured"] == 0
    assert metrics.main(["--events", str(events)]) == 0
    out = capsys.readouterr().out
    assert "costo" in out and "$0.05" in out and "costo real en el período: $0.05 en 2 turnos con uso reportado" in out


def test_cuarentena_tres_fallos_seguidos_de_voto_hoy():
    def done(author, rc, turn="api", timed_out=False):
        return {"event": "trigger_done", "author": author, "turn": turn, "rc": rc, "timed_out": timed_out}
    events = [done("casper", 1), done("casper", 1), done("casper", 0, timed_out=True),
              done("melchior", 1), done("melchior", 1), done("melchior", 0),
              done("balthasar", 1, turn="execute"), done("balthasar", 1, turn="execute"), done("balthasar", 1, turn="execute"),
              done("kimi", 1, turn="synthesis"), done("kimi", 1, turn="synthesis"), done("kimi", 1, turn="synthesis")]
    q = metrics.quarantined_seats(events)
    assert set(q) == {"casper"}, "melchior se recuperó; ejecutor y síntesis no son turnos de voto"
    assert "3 turnos de voto fallidos" in q["casper"]
    assert metrics.quarantined_seats([done("casper", 1), done("casper", 1)]) == {}
