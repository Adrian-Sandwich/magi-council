"""Salida JSON de los CLIs (cli_output.py): texto de la respuesta y uso real
sin claves API. Formatos verificados contra claude 2.x y codex 0.x el
2026-09-21; kimi no reporta uso."""
import json

import pytest

import cli_output


def test_claude_json_trae_texto_tokens_de_contexto_y_costo():
    obj = {"type": "result", "subtype": "success", "is_error": False, "result": "POSITION: yes\nok",
           "total_cost_usd": 0.223703,
           "usage": {"input_tokens": 2, "cache_creation_input_tokens": 22264, "cache_read_input_tokens": 100,
                     "output_tokens": 4}}
    raw = "[warn] algo en stderr\n" + json.dumps(obj) + "\n"
    parsed = cli_output.parse("claude-json", raw)
    assert parsed["text"] == "POSITION: yes\nok" and parsed["error"] is None
    assert parsed["input_tokens"] == 22366, "entrada = nuevo + escrito en caché + leído de caché"
    assert parsed["cached_input_tokens"] == 100 and parsed["output_tokens"] == 4
    assert parsed["cost_usd"] == 0.223703


def test_claude_json_con_is_error_marca_el_error():
    obj = {"type": "result", "subtype": "error", "is_error": True,
           "result": "You've hit your session limit · resets 12:20am"}
    parsed = cli_output.parse("claude-json", json.dumps(obj))
    assert parsed["error"].startswith("You've hit your session limit") and parsed["input_tokens"] is None


def test_codex_jsonl_junta_mensajes_del_agente_y_usa_el_uso_del_turno():
    lines = [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "pensando"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Revisé el repo."}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "POSITION: no\nporque"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12916, "cached_input_tokens": 9984,
                                             "cache_write_input_tokens": 0, "output_tokens": 5,
                                             "reasoning_output_tokens": 0}},
    ]
    raw = "\n".join(json.dumps(line) for line in lines) + "\n"
    parsed = cli_output.parse("codex-jsonl", raw)
    assert parsed["text"] == "Revisé el repo.\n\nPOSITION: no\nporque"
    assert parsed["input_tokens"] == 12916 and parsed["cached_input_tokens"] == 9984 and parsed["output_tokens"] == 5
    assert parsed["cost_usd"] is None and parsed["error"] is None


def test_codex_jsonl_error_y_texto_sin_json():
    raw = json.dumps({"type": "error", "message": "stream disconnected"}) + "\n"
    assert cli_output.parse("codex-jsonl", raw)["error"] == "stream disconnected"
    plain = cli_output.parse("codex-jsonl", "salida rota sin json")
    assert plain["text"] == "salida rota sin json" and plain["input_tokens"] is None
    assert cli_output.parse(None, "hola")["text"] == "hola"
    assert cli_output.parse("text", "hola")["input_tokens"] is None


def test_flags_por_formato():
    assert cli_output.flags(None) == [] and cli_output.flags("text") == []
    assert cli_output.flags("claude-json") == ["--output-format", "json"]
    assert cli_output.flags("codex-jsonl") == ["--json"]
    with pytest.raises(ValueError):
        cli_output.flags("marciano")


def test_kimi_stream_json_deja_solo_la_respuesta_final():
    """kimi emite un objeto por línea: banner de versión, llamadas a
    herramientas sin `content`, resultados de herramienta y, al final, la
    respuesta. Sin esto el voto arrastraba «kimi version 0.42.0» y el eco."""
    lines = [
        {"role": "meta", "type": "system.version", "version": "0.42.0"},
        {"role": "assistant", "tool_calls": [{"type": "function", "id": "t1",
                                              "function": {"name": "Read", "arguments": '{"path": "nota.txt"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "1\tel secreto es 4242"},
        {"role": "assistant", "content": "El número es 4242.\n\nPOSITION: yes"},
        {"role": "meta", "type": "session.resume_hint", "session_id": "s1",
         "content": "To resume this session: kimi -r s1"},
    ]
    raw = "\n".join(json.dumps(line) for line in lines) + "\n"
    parsed = cli_output.parse("kimi-stream-json", raw)
    assert parsed["text"] == "El número es 4242.\n\nPOSITION: yes"
    assert "kimi version" not in parsed["text"] and "resume" not in parsed["text"]
    assert parsed["input_tokens"] is None and parsed["cost_usd"] is None, "kimi no reporta uso"
    assert parsed["error"] is None
    assert cli_output.flags("kimi-stream-json") == ["--output-format", "stream-json"]


def test_kimi_stream_json_sin_respuesta_devuelve_el_texto_crudo():
    solo_meta = json.dumps({"role": "meta", "type": "system.version", "version": "0.42.0"})
    assert cli_output.parse("kimi-stream-json", solo_meta)["text"] == solo_meta, "sin respuesta, no inventes una vacía"
    roto = cli_output.parse("kimi-stream-json", "kimi murió sin json")
    assert roto["text"] == "kimi murió sin json"
    con_error = json.dumps({"role": "error", "content": "quota exceeded"})
    assert cli_output.parse("kimi-stream-json", con_error)["error"] == "quota exceeded"


def test_build_command_pone_los_flags_donde_cada_cli_los_acepta():
    """kimi toma el valor de `-p` como prompt: un flag entre medio se lee como
    prompt y el resto como comando («unknown command stream-json»). Por eso
    sus flags van al final; los de claude y codex, antes del prompt."""
    assert cli_output.build_command("kimi-stream-json", "kimi", ["-p"], "leé el archivo") == \
        ["kimi", "-p", "leé el archivo", "--output-format", "stream-json"]
    assert cli_output.build_command("codex-jsonl", "codex", ["exec", "-m", "x"], "-") == \
        ["codex", "exec", "-m", "x", "--json", "-"]
    assert cli_output.build_command("claude-json", "claude", ["-p"], None) == \
        ["claude", "-p", "--output-format", "json"], "stdin-only: sin argumento de prompt"
    assert cli_output.build_command(None, "kimi", ["-p"], "hola") == ["kimi", "-p", "hola"]
    with pytest.raises(ValueError):
        cli_output.build_command("marciano", "x", [], None)
