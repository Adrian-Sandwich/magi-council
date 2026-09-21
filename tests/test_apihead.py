"""Tests del ejecutor de asientos API (apihead.py): parseo del voto
estructurado (tag POSITION, truco de fshiori/magi) y chat contra un endpoint
OpenAI-compatible de mentira. Nada acá toca Ollama de verdad."""

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import apihead


class _Handler(BaseHTTPRequestHandler):
    """Devuelve una respuesta OpenAI-compatible fija y guarda el último request.
    `script` (lista de (status, cuerpo, headers)) se consume en orden antes
    de caer en `canned`: sirve para 429 → 200 y compañía."""
    canned = {"choices": [{"message": {"content": "POSITION: yes\nPorque sí."}}]}
    script: list = []
    last_payload = None
    last_headers = None
    last_path = None
    requests = 0

    def do_POST(self):
        cls = type(self)
        length = int(self.headers.get("Content-Length", 0))
        cls.last_payload = json.loads(self.rfile.read(length))
        cls.last_headers = {k.lower(): v for k, v in self.headers.items()}  # urllib capitaliza
        cls.last_path = self.path
        cls.requests += 1
        status, payload, extra = cls.script.pop(0) if cls.script else (200, cls.canned, {})
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_endpoint(monkeypatch):
    _Handler.script = []
    _Handler.requests = 0
    monkeypatch.setattr(apihead, "_sleep", lambda s: None)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


# ------------------------------------------------------------ parse_vote

def test_parse_vote_extrae_position_y_razonamiento():
    v = apihead.parse_vote("POSITION: no\nNo hay evidencia de egress.")
    assert v["position"] == "no"
    assert v["conditions"] is None
    assert "egress" in v["body"]


def test_parse_vote_conditional_con_conditions():
    v = apihead.parse_vote(
        "POSITION: conditional\n"
        "CONDITIONS: si hay egress; si el log es completo\n"
        "Resto del razonamiento."
    )
    assert v["position"] == "conditional"
    assert v["conditions"] == ["si hay egress", "si el log es completo"]


def test_parse_vote_case_insensitive():
    assert apihead.parse_vote("position: YES\nok")["position"] == "yes"


@pytest.mark.parametrize('text', ['', 'Creo que sí.', 'POSITION: yes|no|conditional|info'])
def test_parse_vote_requires_explicit_vote(text):
    with pytest.raises(ValueError, match='POSITION'):
        apihead.parse_vote(text)


# ------------------------------------------------------------ chat

def test_chat_postea_openai_compatible(fake_endpoint):
    text = apihead.chat(fake_endpoint, "qwen3", "system de prueba", "user de prueba", timeout_secs=5)
    assert text.startswith("POSITION: yes")

    payload = _Handler.last_payload
    assert payload["model"] == "qwen3"
    assert payload["messages"][0] == {"role": "system", "content": "system de prueba"}
    assert payload["messages"][1] == {"role": "user", "content": "user de prueba"}
    assert payload["stream"] is False


def test_chat_falla_si_el_endpoint_no_responde(monkeypatch):
    waits = []
    monkeypatch.setattr(apihead, "_sleep", waits.append)
    with pytest.raises((urllib.error.URLError, OSError)) as exc:
        apihead.chat("http://127.0.0.1:1/v1", "qwen3", "s", "u", timeout_secs=2)
    assert exc.value.attempts == apihead.RETRY_ATTEMPTS and waits == [2.0, 4.0], "backoff exponencial"


# ------------------------------------------------------------ prompts y run_turn

def _decision():
    return {
        "id": 3, "title": "¿Hubo SQLi?", "artifact": "/repo/auth.log",
        "protocol": "critique", "round": 2,
    }


def test_prompt_api_inlinea_journal_y_contrato_de_respuesta():
    journal = [
        {"author": "melchior", "kind": "posicion", "body": "sí, hay evidencia"},
        {"author": "balthasar", "kind": "posicion", "body": "no me convence"},
    ]
    system, user = apihead.build_api_prompt("melchior", _decision(), journal)
    assert "MELCHIOR" in system
    assert "POSITION: yes|no|conditional|info" in system
    assert "Decisión #3" in user and "¿Hubo SQLi?" in user
    assert "/repo/auth.log" in user
    assert "melchior" in user and "balthasar" in user


def test_prompt_api_trunca_cuerpos_largos_del_journal():
    journal = [{"author": "a", "kind": "posicion", "body": "x" * 10000}]
    _, user = apihead.build_api_prompt("melchior", _decision(), journal)
    assert "x" * (apihead.BODY_CHARS + 1) not in user


def test_run_turn_completo_con_endpoint_falso(fake_endpoint):
    seat = {"seat": "casper", "type": "api", "model": "qwen3", "base_url": fake_endpoint}
    journal = [{"author": "adrian", "kind": "analisis", "body": "la pregunta"}]
    vote = apihead.run_turn(seat, _decision(), journal)
    assert vote["position"] == "yes"
    assert "Porque sí." in vote["body"]


def test_strip_echo_quita_prompt_y_banner_de_codex():
    """codex exec imprime prompt + metadatos + respuesta repetida: el journal
    y el voto quieren sólo la respuesta del asistente."""
    prompt = "Sos CASPER•3...\n\nDecisión #13... Votá."
    stdout = (
        "OpenAI Codex v0.154.0\n--------\nworkdir: C:\repo\nsession id: abc\n"
        "--------\nuser\n" + prompt + "\ncodex\nPOSITION: yes\n\nLa respuesta útil.\n"
        "tokens used 4,407\nPOSITION: yes\n\nLa respuesta útil.\n"
    )
    out = apihead.strip_echo(stdout, prompt)
    assert out == "POSITION: yes\n\nLa respuesta útil."

    # texto sin eco ni banner queda intacto
    crudo = "POSITION: no\n\nmi razonamiento"
    assert apihead.strip_echo(crudo, prompt) == crudo


def test_sin_artefacto_no_manda_a_investigar_el_repo():
    """Para una pregunta filosófica el cwd es el de MAGI: la cabeza terminaba
    leyendo personas.py y haciendo grep de 'humano' en el código (decisión
    #26). Sin artefacto se razona desde el journal, no desde el disco."""
    base = {"id": 26, "protocol": "adaptive", "round": 1, "title": "¿qué es la condición de ser humano?"}
    sin = apihead.build_inline_active_prompt("casper", dict(base, artifact=None), [], None, "C:/magi")
    con = apihead.build_inline_active_prompt("casper", dict(base, artifact="C:/repo"), [], None, "C:/repo")
    assert "NO busques archivos" in sin and "git status" not in sin
    assert "INVESTIGÁ con tus herramientas" in con and "NO busques archivos" not in con
    assert "POSITION: yes|no|conditional|info" in sin


# ------------------------------------------------------------ proveedores, reintentos, uso y costo

def _seat(**kw):
    return {"seat": "casper", "type": "api", "model": "qwen3", **kw}


def test_429_y_503_se_reintentan_respetando_retry_after(fake_endpoint, monkeypatch):
    waits = []
    monkeypatch.setattr(apihead, "_sleep", waits.append)
    _Handler.script = [(429, {"error": "rate"}, {"Retry-After": "1"}), (503, {"error": "down"}, {})]
    result = apihead.complete(_seat(base_url=fake_endpoint), "s", "u", 5)
    assert result["text"].startswith("POSITION: yes") and result["attempts"] == 3
    assert waits == [1.0, 4.0], "Retry-After manda; sin él, backoff 2·2^(n-1)"
    assert _Handler.requests == 3


def test_401_no_se_reintenta_y_trae_el_estado(fake_endpoint):
    _Handler.script = [(401, {"error": {"message": "bad key"}}, {})]
    with pytest.raises(apihead.ApiError) as exc:
        apihead.complete(_seat(base_url=fake_endpoint), "s", "u", 5)
    assert exc.value.status == 401 and exc.value.attempts == 1 and "bad key" in str(exc.value)
    assert _Handler.requests == 1


def test_tres_fallos_seguidos_agotan_los_reintentos(fake_endpoint):
    _Handler.script = [(500, {}, {}), (502, {}, {}), (504, {}, {})]
    with pytest.raises(apihead.ApiError) as exc:
        apihead.complete(_seat(base_url=fake_endpoint), "s", "u", 5)
    assert exc.value.status == 504 and exc.value.attempts == 3


def test_openai_manda_bearer_y_devuelve_uso_y_costo(fake_endpoint, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _Handler.script = [(200, {"choices": [{"message": {"content": "POSITION: no\nnope"}}],
                              "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}, {})]
    seat = _seat(provider="openai", base_url=fake_endpoint, model="gpt-5",
                 pricing={"input_per_mtok": 2.5, "output_per_mtok": 10})
    result = apihead.complete(seat, "s", "u", 5)
    assert _Handler.last_headers["authorization"] == "Bearer sk-test"
    assert _Handler.last_path.endswith("/chat/completions")
    assert result["input_tokens"] == 1200 and result["output_tokens"] == 300
    assert result["cost_usd"] == round(1200 / 1e6 * 2.5 + 300 / 1e6 * 10, 6) and result["provider"] == "openai"


def test_anthropic_usa_messages_api_con_x_api_key(fake_endpoint, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _Handler.script = [(200, {"content": [{"type": "text", "text": "POSITION: conditional\nCONDITIONS: a; b\nok"}],
                              "usage": {"input_tokens": 800, "output_tokens": 120}}, {})]
    seat = _seat(provider="anthropic", base_url=fake_endpoint, model="claude-opus-4-1", max_tokens=512)
    stats = {}
    vote = apihead.run_turn(seat, {"id": 3, "title": "t", "artifact": None, "protocol": "vote", "round": 1},
                            [{"author": "adrian", "kind": "analisis", "body": "la pregunta"}], stats=stats)
    assert _Handler.last_path.endswith("/v1/messages")
    assert _Handler.last_headers["x-api-key"] == "sk-ant-test" and _Handler.last_headers["anthropic-version"]
    payload = _Handler.last_payload
    assert payload["max_tokens"] == 512 and payload["messages"] == [{"role": "user", "content": payload["messages"][0]["content"]}]
    assert "system" in payload and "casper" in payload["system"].lower() or payload["system"]
    assert vote["position"] == "conditional" and vote["conditions"] == ["a", "b"]
    assert stats["input_tokens"] == 800 and stats["output_tokens"] == 120 and stats["cost_usd"] is None
    assert vote["usage"]["provider"] == "anthropic"


def test_is_configured_explica_que_falta(monkeypatch):
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    ok, why = apihead.is_configured(_seat(provider="moonshot", model="kimi-k2"))
    assert not ok and "MOONSHOT_API_KEY" in why
    monkeypatch.setenv("MOONSHOT_API_KEY", "x")
    ok, why = apihead.is_configured(_seat(provider="moonshot", model="kimi-k2"))
    assert ok and "moonshot" in why
    assert apihead.provider_of(_seat(provider="moonshot"))["base_url"] == "https://api.moonshot.ai/v1"
    assert not apihead.is_configured(_seat(model=None))[0]
    assert not apihead.is_configured(_seat(provider="marciano"))[0]
    # local (Ollama): base_url y nada de clave
    assert apihead.is_configured(_seat(base_url="http://127.0.0.1:11434/v1"))[0]
    with pytest.raises(apihead.ApiError) as exc:
        apihead.complete(_seat(provider="openai", api_key_env="NO_EXISTE_ESTA_VARIABLE"), "s", "u", 1)
    assert exc.value.status == 401


def test_heads_is_active_para_api_requiere_la_clave(monkeypatch):
    import heads
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seat = _seat(provider="anthropic", model="claude-opus-4-1")
    assert not heads.is_active(seat)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert heads.is_active(seat)
