"""Modos de persona: off (control), rhetorical (eje + sesgo) y behavioral
(eje + sesgo + obligación verificable). El experimento persona_ab.py se
prueba en sus partes puras: elección de decisiones y métricas."""

import pytest

import persona_ab
import personas


def test_modos_de_persona_cambian_el_prompt(monkeypatch):
    off = personas.system_prompt("melchior", "off")
    rhetorical = personas.system_prompt("melchior", "rhetorical")
    behavioral = personas.system_prompt("melchior", "behavioral")
    assert "MELCHIOR" in off and "verdad técnica" not in off and "sesgo" not in off
    assert "verdad técnica" in rhetorical and "frialdad" in rhetorical and "obligación" not in rhetorical
    assert behavioral.startswith(rhetorical) and "Tu obligación en cada turno" in behavioral
    assert "archivo:línea" in behavioral and "no votes yes" in behavioral
    # cada obligación es distinta y agnóstica de dominio
    obligations = {s: personas.PERSONAS[s]["obligacion"] for s in personas.PERSONAS}
    assert len(set(obligations.values())) == 3
    assert "pasaje" in obligations["melchior"] and "irreversible" in obligations["balthasar"]
    assert "más barato" in obligations["casper"]


def test_modo_vigente_sale_del_entorno_con_fallback(monkeypatch):
    monkeypatch.delenv("MAGI_PERSONA_MODE", raising=False)
    assert personas.mode() == personas.DEFAULT_MODE
    monkeypatch.setenv("MAGI_PERSONA_MODE", "behavioral")
    assert personas.mode() == "behavioral"
    assert "Tu obligación" in personas.system_prompt("casper")
    monkeypatch.setenv("MAGI_PERSONA_MODE", "gendo")
    assert personas.mode() == personas.DEFAULT_MODE
    with pytest.raises(ValueError):
        personas.system_prompt("gendo")


def test_pick_decisions_mezcla_con_y_sin_repo_y_descarta_control():
    rows = [
        {"id": 1, "title": "¿Conviene migrar el esquema a jsonb ahora?", "artifact": "/r", "votes": 3},
        {"id": 2, "title": "seguí", "artifact": None, "votes": 3},
        {"id": 3, "title": "cual es la naturaleza de dios?", "artifact": None, "votes": 3},
        {"id": 4, "title": "revisa este repo y dime que mejorar", "artifact": "/r", "votes": 2},
        {"id": 5, "title": "que es la condicion de ser humano?", "artifact": None, "votes": 3},
        {"id": 6, "title": "¿Mergeamos la rama de auth con las condiciones?", "artifact": "/r", "votes": 3},
    ]
    chosen = persona_ab.pick_decisions(rows, 4)
    assert [r["id"] for r in chosen] == [1, 3, 5, 6]
    assert [r["id"] for r in persona_ab.pick_decisions(rows, 2)] == [5, 6]


def test_metrics_por_modo():
    results = [
        # off: todos iguales y mismo vocabulario
        {"decision_id": 1, "mode": "off", "seat": "melchior", "position": "yes", "body": "migrar esquema jsonb ahora conviene"},
        {"decision_id": 1, "mode": "off", "seat": "balthasar", "position": "yes", "body": "migrar esquema jsonb ahora conviene"},
        {"decision_id": 1, "mode": "off", "seat": "casper", "position": "yes", "body": "migrar esquema jsonb ahora conviene"},
        # behavioral: discrepan, recitan y usan vocabulario distinto
        {"decision_id": 1, "mode": "behavioral", "seat": "melchior", "position": "conditional", "body": "verifiqué los hechos: schema.sql línea 12"},
        {"decision_id": 1, "mode": "behavioral", "seat": "balthasar", "position": "no", "body": "el daño es irreversible: pérdida de datos"},
        {"decision_id": 1, "mode": "behavioral", "seat": "casper", "position": "yes", "body": "el operador quiere velocidad; lo más barato"},
        {"decision_id": 2, "mode": "behavioral", "seat": "melchior", "position": None, "body": "sin voto"},
    ]
    m = persona_ab.metrics(results)
    assert m["off"]["pair_agreement"] == 1.0 and m["off"]["unanimous_rate"] == 1.0
    assert m["off"]["argument_diversity"] == 0.0 and m["off"]["axis_recited_rate"] == 0.0
    assert m["behavioral"]["votes"] == 3 and m["behavioral"]["decisions"] == 1
    assert m["behavioral"]["pair_agreement"] == 0.0 and m["behavioral"]["unanimous_rate"] == 0.0
    assert m["behavioral"]["axis_recited_rate"] == 1.0 and m["behavioral"]["argument_diversity"] == 1.0
    assert m["behavioral"]["positions"]["balthasar"] == {"no": 1}
    text = persona_ab.render({"seat_config": "casper", "decisions": 2, "modes": m})
    assert "behavioral" in text and "100%" in text


def test_seat_configs_un_solo_proveedor_o_el_consejo_real(monkeypatch):
    """Las dos mitades del experimento: con un proveedor en los tres asientos
    la única variable es la persona; con `--mixed` se suma el proveedor, que
    es lo que hay que separar. Un asiento API o sin journal inline no sirve:
    el experimento vota por stdout de un CLI."""
    import heads
    registry = {
        "melchior": {"seat": "melchior", "name": "kimi", "type": "cli", "journal": "inline", "bin": "/k"},
        "balthasar": {"seat": "balthasar", "name": "codex", "type": "cli", "journal": "inline", "bin": "/c"},
        "casper": {"seat": "casper", "name": "claude", "type": "cli", "journal": "inline", "bin": "/cl"},
    }
    monkeypatch.setattr(heads, "seat_by_name", lambda name: registry.get(name))

    configs, label = persona_ab.seat_configs("casper", mixed=False)
    assert {s: c["bin"] for s, c in configs.items()} == {s: "/cl" for s in persona_ab.SEATS}
    assert "claude" in label and "tres asientos" in label

    configs, label = persona_ab.seat_configs(None, mixed=True)
    assert [configs[s]["bin"] for s in persona_ab.SEATS] == ["/k", "/c", "/cl"]
    assert label.startswith("mixto:") and "melchior=kimi" in label and "casper=claude" in label

    registry["casper"] = {"seat": "casper", "type": "api", "model": "x", "base_url": "u"}
    with pytest.raises(SystemExit):
        persona_ab.seat_configs("casper", mixed=False)
    with pytest.raises(SystemExit):
        persona_ab.seat_configs(None, mixed=True)


def test_render_dice_que_configuracion_corrio():
    base = {"decisions": 20, "seat_config": "mixto: melchior=kimi, balthasar=codex, casper=claude",
            "mixed": True, "modes": {"off": {"votes": 60, "pair_agreement": 0.5, "unanimous_rate": 0.3,
                                             "axis_recited_rate": 0.1, "argument_diversity": 0.9,
                                             "positions": {s: {"yes": 20} for s in persona_ab.SEATS}}}}
    text = persona_ab.render(base)
    assert "20 decisiones × 1 modos, mixto: melchior=kimi" in text
    assert "cada asiento con su proveedor" in text
    solo = persona_ab.render(dict(base, mixed=False, seat_config="casper (claude) en los tres asientos"))
    assert "mismo modelo en los tres asientos" in solo


def test_scratch_config_deja_correr_a_codex_fuera_de_un_repo():
    """El experimento vota en un tempdir: codex exec aborta ahí con «Not
    inside a trusted directory» salvo que se le pase --skip-git-repo-check."""
    codex = {"seat": "balthasar", "name": "codex", "bin": "/c",
             "args": ["exec", "-m", "gpt-5.6-luna", "--sandbox", "workspace-write"]}
    config = persona_ab.scratch_config(codex, "melchior")
    assert config["args"][-1] == "--skip-git-repo-check" and config["seat"] == "melchior"
    assert codex["args"][-1] == "workspace-write", "no muta la config del registry"
    assert persona_ab.scratch_config(config, "melchior")["args"].count("--skip-git-repo-check") == 1
    claude = {"seat": "casper", "bin": "/cl", "args": ["-p", "--model", "opus"]}
    assert persona_ab.scratch_config(claude, "casper")["args"] == ["-p", "--model", "opus"]


def test_pick_decisions_descarta_planes_del_ejecutor_y_sus_revisiones():
    """Los planes de producción y sus revisiones son tareas mecánicas: las
    tres cabezas aprueban «agregar type hints» por lo mismo. Contarlas como
    deliberaciones infla el acuerdo y hunde la diversidad sin que la persona
    intervenga (la corrida del 2026-09-24 traía 10 de 20 así)."""
    rows = [
        {"id": 1, "title": "¿Conviene migrar el esquema a jsonb ahora?", "artifact": "/r", "votes": 3},
        {"id": 2, "title": "Implementa: agregar type hints a inventario/utils.py", "artifact": "/r", "votes": 3},
        {"id": 3, "title": "Revisar implementación de #2: agregar type hints", "artifact": "/r", "votes": 3},
        {"id": 4, "title": "que es la condicion de ser humano?", "artifact": None, "votes": 3},
        {"id": 5, "title": "Preparar la migración del tablero a otro host", "artifact": "/r",
         "votes": 3, "production": True},
    ]
    assert [r["id"] for r in persona_ab.pick_decisions(rows, 10)] == [1, 4]
    assert persona_ab.is_execution_plan(rows[1]) and persona_ab.is_execution_plan(rows[4])
    assert not persona_ab.is_execution_plan(rows[0])


def test_compare_arma_la_tabla_de_varias_corridas():
    """La comparación entre corridas es lo que separa persona de proveedor:
    con un solo modelo la única variable es la persona; con los tres, se suma
    el proveedor."""
    def report(votes, diversity):
        return {"modes": {"off": {"votes": votes, "pair_agreement": 0.5, "unanimous_rate": 0.25,
                                  "axis_recited_rate": 0.1, "argument_diversity": diversity,
                                  "positions": {}}}}
    tabla = persona_ab.compare([("codex en los tres asientos", report(60, 0.80)),
                                ("mixto", report(57, 0.93))])
    filas = tabla.splitlines()
    assert filas[0].startswith("| corrida |") and filas[1].count("---") == 7
    assert "| codex en los tres asientos | off | 60 | 50% | 25% | 10% | 80% |" in tabla
    assert "| mixto | off | 57 | 50% | 25% | 10% | 93% |" in tabla
