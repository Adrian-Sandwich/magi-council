"""Optional browser checks with intercepted requests: never contact the live board."""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page(allow_real_processes):
    executable = os.environ.get("CLAMI_BROWSER_PATH")
    if not executable:
        pytest.skip("Set CLAMI_BROWSER_PATH to run the optional browser checks")
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.add_init_script("window.EventSource = class {constructor() {window.feed = this;}}")

        def route(request):
            filename = (request.request.url.rsplit("/", 1)[-1] or "index.html").split("?", 1)[0]
            path = ROOT / "debate-mcp" / "ui" / filename
            if filename not in ("index.html", "app.js", "sound.js", "style.css"):
                request.abort()
                return
            types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            request.fulfill(body=path.read_bytes(), content_type=types[path.suffix])

        page.route("**/*", route)
        page.goto("http://magi.test/")
        yield page
        browser.close()


def snapshot(status="open"):
    return {"chat": [], "decisions": [{"id": 4, "status": status, "round": 1,
        "protocol": "adaptive", "title": "Make the council easier to use", "confidence": None,
        "badge": {"text": "DELIBERATING", "color": "#ff8d00", "flicker": False},
        "seats": [{"seat": seat, "voted": False, "body": ""}
                  for seat in ("melchior", "balthasar", "casper")], "journal": []}]}


def feed(page, value):
    page.evaluate("data => window.feed.onmessage({data: JSON.stringify(data)})", value)


def test_joint_answer_replaces_transcript_and_marks_partial_review(page):
    data = snapshot('closed')
    d = data['decisions'][0]
    for seat in d['seats']:
        seat.update(voted=True, position='info', body='exec PRIVATE RAW LOG')
    d['synthesis'] = {'status': 'partial', 'answer': 'Una respuesta conjunta <script>literal</script>',
        'agreements': ['Coincidimos en cuidar los datos'], 'differences': ['Falta acordar el plazo'],
        'open_questions': [], 'cycle': 2,
        'reviews': [{'seat':'casper','approve':False,'feedback':'Falta justificar el costo'}]}
    feed(page, data)
    assert page.locator('#summary-lead').inner_text() == d['synthesis']['answer']
    assert 'PRIVATE RAW LOG' not in page.locator('#summary-card').inner_text()
    assert 'no todas las cabezas' in page.locator('#summary-content').inner_text()
    assert page.locator('#summary-content script').count() == 0
    assert page.locator('#detail-panel').get_attribute('open') is None


def test_draft_visible_while_head_review_is_pending(page):
    data=snapshot('closed')
    data['decisions'][0]['synthesis']={'status':'generating','answer':'Respuesta provisional útil',
        'cycle':1,'phase':'reviewing','current_head':'melchior','reviews':[],
        'agreements':[],'differences':[],'open_questions':[]}
    feed(page,data)
    assert page.locator('#summary-lead').inner_text() == 'Respuesta provisional útil'
    assert 'Borrador en revisión' in page.locator('#summary-title').inner_text()
    assert 'melchior' in page.locator('#summary-content').inner_text()


def test_editorial_approval_does_not_hide_unresolved_content(page):
    data=snapshot('closed')
    data['decisions'][0]['synthesis']={'status':'reviewed','answer':'Respuesta con desacuerdos',
        'cycle':1,'reviews':[],'agreements':[],'differences':[],'open_questions':[],
        'content_state':'budget_exhausted','content_consensus':False}
    feed(page,data)
    assert 'Sin consenso de contenido' in page.locator('#summary-title').inner_text()
    assert 'se agotaron las rondas' in page.locator('#summary-content').inner_text()


def test_outcome_retry_preserves_report_and_request_identity(page):
    feed(page,snapshot('closed'))
    page.locator('#outcome-panel summary').click()
    page.locator('#outcome-status').select_option('failed')
    page.locator('#outcome-observation').fill('Sigue fallando al reiniciar')
    page.locator('#outcome-evidence').fill('Prueba en mi equipo')
    sent = []
    def fail(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=503,content_type='application/json',body='{"error":"Temporal"}')
    page.route('**/outcome',fail)
    page.locator('#outcome-save').click()
    page.wait_for_function("document.getElementById('outcome-notice').textContent === 'Temporal'")
    assert page.locator('#outcome-observation').input_value() == 'Sigue fallando al reiniciar'
    page.locator('#outcome-save').click()
    page.wait_for_function("!document.getElementById('outcome-save').disabled")
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0]['decision_id'] == 4


def test_composer_targets_actions_and_preserves_failed_drafts(page):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    feed(page, snapshot("split"))
    assert page.locator("#summary-card").is_visible()
    assert page.locator("#detail-panel").get_attribute("open") is None
    assert page.locator("#summary-title").inner_text() == "The council needs your decision"
    page.locator("#c-input").fill("Keep this context")
    page.locator("#sa-ruling").click()
    assert page.locator("#c-send").inner_text() == "Close with my ruling"
    sent = []

    def fail(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=409, content_type="application/json", body='{"error":"Decision changed"}')

    page.route("**/message", fail)
    page.locator("#c-send").click()
    page.wait_for_function("document.getElementById('c-status').textContent.includes('Decision changed')")
    assert sent[-1] == {"mode": "council", "body": "Keep this context", "decision_id": 4, "action": "arbitrate"}
    assert page.locator("#c-input").input_value() == "Keep this context"
    page.locator("#sa-segui").click()
    page.locator("#c-send").click()
    page.wait_for_function("!document.getElementById('c-send').disabled")
    assert sent[-1]["action"] == "resume"
    page.locator("#c-new").click()
    assert len(sent) == 2  # New question prepares a draft; it never sends.
    assert page.locator("#c-send").inner_text() == "Ask council"
    page.locator("#c-repo").fill("C:/work/example")
    assert page.locator("#c-send").inner_text() == "Ask council"
    page.locator("#c-send").click()
    page.wait_for_function("!document.getElementById('c-send').disabled")
    assert sent[-1]["force_new"] is True
    assert "decision_id" not in sent[-1]
    assert sent[-1]["artifact"] == "C:/work/example"
    page.evaluate("window.feed.onerror()")
    assert page.locator("#c-send").is_disabled()
    assert not errors


@pytest.mark.parametrize("browse", [False, True])
def test_repository_selection_after_closed_decision_starts_analysis(page, browse):
    data = snapshot("closed")
    data["decisions"][0]["artifact"] = "C:/work/magi"
    feed(page, data)
    assert page.locator("#c-send").inner_text() == "Continue this decision"
    assert "C:/work/magi" in page.locator("#c-intent").inner_text()
    if browse:
        page.route("**/fs", lambda route: route.fulfill(
            content_type="application/json", body=json.dumps({
                "path": "C:/work/cutulu", "parent": "C:/work", "dirs": []})))
        page.locator("#c-browse").click()
        page.locator("#fs-use").click()
    else:
        page.locator("#c-repo").fill("C:/work/cutulu")
    page.locator("#c-input").fill("Analyze this repository")
    assert "C:/work/cutulu" in page.locator("#c-intent").inner_text()
    sent = []

    def receive(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=201, content_type="application/json",
                      body='{"action":"opened","decision_id":5,"production":false}')

    page.route("**/message", receive)
    page.locator("#c-send").click()
    page.wait_for_function("document.getElementById('c-status').textContent.includes('#5 opened')")
    assert sent == [{"mode": "council", "body": "Analyze this repository",
                     "force_new": True, "artifact": "C:/work/cutulu"}]


def test_natural_followup_evolves_approved_analysis_to_execution(page):
    data = snapshot("closed")
    data["decisions"][0].update(ruling="conditional", artifact="C:/work/cutulu")
    feed(page, data)
    page.locator("#c-input").fill("vamos con tu plan")
    assert page.locator("#c-send").inner_text() == "Implement approved plan"
    assert "isolated execution" in page.locator("#c-intent").inner_text()
    sent = []
    page.route("**/message", lambda route: (
        sent.append(route.request.post_data_json),
        route.fulfill(status=201, content_type="application/json",
                      body='{"action":"execution_requested","decision_id":4}')
    ))
    page.locator("#c-send").click()
    page.wait_for_function("document.getElementById('c-status').textContent.includes('isolated execution')")
    assert sent == [{"mode": "council", "body": "vamos con tu plan",
                     "decision_id": 4, "action": "execute"}]


def test_failed_head_shows_error_and_retry_preserves_draft(page):
    data = snapshot()
    error = {'id': 'attempt-1', 'round': 1, 'message': 'Tiempo agotado esperando la respuesta.'}
    data['decisions'][0]['turn_errors'] = {'melchior': error}
    data['decisions'][0]['seats'][0]['error'] = error
    feed(page, data)
    assert page.locator('.wise-man.melchior .thinking-tag').inner_text() == 'ERROR'
    assert page.locator('.wise-man.melchior .flicker').count() == 0
    assert 'Tiempo agotado' in page.locator('#summary-lead').inner_text()
    page.locator('#c-input').fill('Keep this draft')
    sent = []

    def retry(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type='application/json', body='{"action":"retried"}')

    page.route('**/retry-turns', retry)
    page.locator('#c-retry').click()
    page.wait_for_function("document.getElementById('c-status').textContent.includes('Reintento solicitado')")
    assert sent == [{'decision_id': 4, 'errors': {'melchior': 'attempt-1'}}]
    assert page.locator('#c-input').input_value() == 'Keep this draft'


def test_sound_transition_dedup_keyboard_and_mobile(page):
    assert page.locator("#sound-toggle").get_attribute("aria-pressed") == "false"
    assert page.locator(".wise-man").count() == 3
    page.locator("#sound-toggle").click()
    assert page.locator("#sound-toggle").get_attribute("aria-pressed") == "true"
    page.evaluate("() => { window.cues = []; MagiSound.play = kind => window.cues.push(kind); }")
    data = snapshot()
    feed(page, data)
    assert page.evaluate("window.cues") == []
    data["decisions"][0]["seats"][0].update(voted=True, position="yes")
    feed(page, data)
    feed(page, data)
    assert page.evaluate("window.cues") == ["vote"]
    data["decisions"][0].update(status="executing", execution_state="pending")
    feed(page, data)
    assert page.evaluate("window.cues") == ["vote", "machinery"]
    page.evaluate("window.feed.onerror()")
    data["decisions"][0]["status"] = "split"
    feed(page, data)
    assert page.evaluate("window.cues") == ["vote", "machinery"]
    page.locator(".wise-man").first.focus()
    page.keyboard.press("Enter")
    assert page.locator("#modal").is_visible()
    page.keyboard.press("Escape")
    assert not page.locator("#modal").is_visible()
    page.screenshot(path=str(ROOT / "experiments" / "ux-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ROOT / "experiments" / "ux-mobile.png"), full_page=True)


def test_auto_scroll_respeta_la_posicion_de_lectura(page):
    """0b9b9c0: el auto-scroll solo baja si el usuario ya estaba en el fondo.
    Con el scroll arriba, un frame SSE con cambios (un voto) no lo tira;
    en el fondo, un mensaje nuevo baja hasta el final — incluida la línea
    de 'is thinking', que se agrega ANTES de ajustar el scroll."""
    data = snapshot()
    data["decisions"][0]["journal"] = [
        {"author": "melchior", "kind": "posicion", "body": f"hallazgo {i:02d} — línea del journal lo suficientemente larga como para ocupar su renglón",
         "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"}
        for i in range(40)
    ]
    feed(page, data)
    conv = page.locator("#conversation")
    assert conv.evaluate("el => el.scrollHeight > el.clientHeight"), \
        "el journal tiene que desbordar para que la posición del scroll importe"
    # leyendo arriba: un frame con cambio (voto de una cabeza) NO lo baja
    conv.evaluate("el => el.scrollTop = 0")
    data["decisions"][0]["seats"][0].update(voted=True, position="yes")
    feed(page, data)
    assert conv.evaluate("el => el.scrollTop") == 0
    # en el fondo: el mensaje nuevo baja hasta el final, thinking-line incluida
    conv.evaluate("el => el.scrollTop = el.scrollHeight")
    data["decisions"][0]["journal"].append(
        {"author": "casper", "kind": "posicion", "body": "una observación nueva",
         "created_at": "2026-01-01T01:00:00"})
    feed(page, data)
    assert conv.evaluate("el => el.scrollHeight - el.scrollTop - el.clientHeight") <= 1, \
        "en el fondo el scroll tiene que quedar pegado al final, con la línea de thinking a la vista"
