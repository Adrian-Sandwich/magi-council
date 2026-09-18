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
