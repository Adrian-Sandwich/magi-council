import json
import threading

import pytest

import council_synthesis as synthesis


BUNDLE = {'question': '¿Qué hacemos?', 'heads': ['melchior','balthasar','casper'], 'contributions': []}
SEATS = [{'seat': s, 'type': 'api'} for s in BUNDLE['heads']]
DRAFT = {'answer': 'Probar primero, conservando los datos.', 'agreements': ['Conservar datos'],
         'differences': ['El plazo sigue en discusión'], 'open_questions': []}


def test_parser_accepts_one_bounded_next_move_or_none():
    move = {'title': 'Conectar eventos', 'reason': 'Las funciones existen sin uso',
            'expected_result': 'Eventos visibles durante la partida', 'scope': 'Cuatro archivos',
            'risk': 'Medio', 'recommendation': 'discuss'}
    assert synthesis.parse(json.dumps(dict(DRAFT, next_move=move)))['next_move'] == move
    assert synthesis.parse(json.dumps(DRAFT))['next_move'] is None


def test_parser_rejects_unbounded_next_move():
    move = {'title': 'Conectar eventos', 'reason': 'Motivo', 'expected_result': 'Resultado',
            'scope': 'Alcance', 'risk': 'Riesgo', 'recommendation': 'run-forever'}
    with pytest.raises(ValueError):
        synthesis.parse(json.dumps(dict(DRAFT, next_move=move)))


def test_parser_bounds_verbose_but_valid_editor_lists():
    verbose = dict(DRAFT, agreements=[f"punto {i}" for i in range(6)])
    parsed = synthesis.parse(json.dumps(verbose))
    assert parsed["agreements"] == verbose["agreements"][:synthesis.LIST_MAX_ITEMS]


def test_three_reviews_required_and_disagreement_preserved():
    calls = []
    def invoke(seat, prompt):
        calls.append(seat['seat'])
        if 'Redactá una respuesta' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'reviewed'
    assert len(result['reviews']) == 3
    assert result['differences'] == DRAFT['differences']
    assert len(calls) == 5  # borrador + corrector de estilo + 3 revisiones


def test_writer_priority_and_reviews_run_in_parallel():
    barrier = threading.Barrier(3)
    seats = [dict(seat=s, type='api', synthesis_priority=priority)
             for s, priority in zip(BUNDLE['heads'], (30, 20, 10))]
    calls = []
    lock = threading.Lock()

    def invoke(seat, prompt):
        with lock:
            calls.append(seat['seat'])
        if 'Redactá una respuesta' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)
        barrier.wait(timeout=2)
        return json.dumps({'approve': True, 'accept_answer': True, 'feedback': ''})

    result = synthesis.compose(BUNDLE, seats, invoke)
    assert calls[0] == 'casper'
    assert result['status'] == 'reviewed'


def test_dissent_never_becomes_consensus_and_budget_is_bounded():
    calls = []
    def invoke(seat, prompt):
        calls.append(prompt)
        if 'Redactá una respuesta' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': seat['seat'] != 'casper', 'feedback': 'Falta el costo'})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert result['cycle'] == 2
    assert len(calls) == 10  # (borrador + corrector + 3 revisiones) x 2 ciclos
    assert 'Falta el costo' in calls[5]


def test_missing_reviewer_is_not_approval():
    def invoke(seat, prompt):
        return json.dumps(DRAFT if ('Redactá una respuesta' in prompt or 'corrector de estilo' in prompt) else {'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS[:2], invoke)
    assert result['status'] == 'partial'
    assert result['reviews'][-1]['approve'] is False


def test_stale_result_is_not_published():
    class Transaction:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Conn:
        def transaction(self): return Transaction()
        def execute(self, sql, params=()):
            assert not sql.startswith('UPDATE')
            self.sql = sql
            return self
        def fetchone(self):
            if 'SELECT *' in self.sql:
                return {'round': 2, 'status': 'open', 'thread': 'd1'}
            return {'id': 99}
    assert not synthesis.save(Conn(), 1, {'round':1,'status':'closed','message_id':42}, DRAFT)


def test_broken_reviewer_output_is_not_approval():
    calls = []
    def invoke(seat, prompt):
        calls.append(prompt)
        if 'Redactá una respuesta' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)
        return 'Provider error: invalid model'
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'partial'
    assert not any(review['approve'] for review in result['reviews'])
    assert len(calls) == 5
    assert result['cycle'] == 1
    assert result['stop_reason'] == 'review_unavailable'


def test_draft_is_published_before_slow_review_and_survives_failed_revision():
    updates = []
    drafts = 0
    def invoke(seat,prompt):
        nonlocal drafts
        if 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)
        if 'Redactá una respuesta' in prompt:
            drafts += 1
            if drafts == 2:
                raise TimeoutError()
            return json.dumps(DRAFT)
        assert updates[-1]['answer'] == DRAFT['answer']
        assert updates[-1]['current_head'] == 'all heads'
        return json.dumps({'approve':False,'feedback':'Aclarar el alcance'})
    result = synthesis.compose(BUNDLE,SEATS,invoke,progress=updates.append)
    assert result['answer'] == DRAFT['answer']
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'revision_failed'


# ------------------------------------------------------------ estilo editorial

def test_style_issues_detecta_lo_que_salio_mal_en_la_sintesis_real():
    """Una síntesis real: 250 palabras, «desde mi eje… no nos la bancamos»,
    «observer-relative» y una pregunta abierta sobre si la decisión #24
    estaba duplicada en el registro. Ninguna regla es teórica."""
    bad = {'answer': ('Lo divino es una construcción observer-relative. Desde mi eje, la respuesta '
                      'conveniente sería sí, y la rechazo porque no nos la bancamos. ' + 'palabra ' * 260),
           'agreements': ['a'], 'differences': [], 'open_questions': ['Las fuentes registran la DECISIÓN #24 cerrada dos veces'],
           'blocking_conditions': ['distinguir construcción de ilusión'], 'deferred_items': []}
    issues = synthesis.style_issues(bad, conceptual=True)
    assert len(issues) >= 5
    assert any('palabras' in i for i in issues) and any('primera persona' in i for i in issues)
    assert any('jerga' in i for i in issues) and any('registro' in i for i in issues)
    assert any('blocking_conditions' in i for i in issues)
    assert synthesis.style_issues(DRAFT, conceptual=True) == []
    assert synthesis.style_issues(dict(DRAFT, blocking_conditions=['x']), conceptual=False) == []


def test_borrador_con_problemas_de_estilo_se_corrige_antes_de_la_revision():
    prompts = []
    bad = dict(DRAFT, answer='Desde mi eje creo que sí. ' + 'texto ' * 130)

    def invoke(seat, prompt):
        prompts.append(prompt)
        if 'rompe estas reglas de estilo' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(DRAFT)              # el escritor corrige
        if 'Redactá una respuesta' in prompt:
            return json.dumps(bad)                # primer borrador, mal escrito
        assert 'texto texto' not in prompt, 'los revisores no deben leer el borrador sin corregir'
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['status'] == 'reviewed'
    assert result['answer'] == DRAFT['answer'] and result['style_issues'] == []
    assert sum('rompe estas reglas' in p for p in prompts) == 1
    assert len(prompts) == 6  # borrador + corrección de reglas + corrector de estilo + 3 revisiones


def test_pregunta_conceptual_no_lleva_condiciones_y_el_prompt_lo_dice():
    prompts = []

    def invoke(seat, prompt):
        prompts.append(prompt)
        if 'Redactá una respuesta' in prompt or 'corrector de estilo' in prompt:
            return json.dumps(dict(DRAFT, blocking_conditions=['x'], deferred_items=['y']))
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)   # BUNDLE: sin artifact ni production
    assert result['blocking_conditions'] == [] and result['deferred_items'] == []
    assert 'van VACÍOS' in prompts[0] and 'REGLAS DE ESTILO' in prompts[0]
    assert 'tercera persona' in prompts[0]

    prompts.clear()
    code = dict(BUNDLE, artifact='C:/repo', production=True)
    result = synthesis.compose(code, SEATS, invoke)
    assert result['blocking_conditions'] == ['x']
    assert f'como máximo {synthesis.CONDITIONS_MAX_ITEMS} blocking_conditions' in prompts[0]


def test_las_condiciones_de_codigo_tambien_tienen_reglas_de_estilo():
    draft = dict(DRAFT, blocking_conditions=['Distinguir construcción de ilusión.', 'distinguir construcción de ilusión'],
                 deferred_items=['una tarea ' * 20])
    issues = synthesis.style_issues(draft, conceptual=False)
    assert any('repetidas' in i for i in issues) and any('30 palabras' in i for i in issues)
    assert synthesis.style_issues(dict(DRAFT, blocking_conditions=['Agregar playtests/ al .gitignore']), conceptual=False) == []


def test_estilo_telegrafico_siglas_y_meta_salvedades_se_detectan():
    """La respuesta real sobre RSA: fragmentos con dos puntos y punto y coma,
    «OAEP y PSS con errores uniformes» sin explicar, y «anclas públicas» /
    «proyecciones de papers» como salvedades de editor."""
    rsa = {'answer': ('Lo vulnerable de RSA no es el álgebra sino su entorno: azar débil, relleno PKCS#1 v1.5, '
                      'canales laterales; errores de cálculo. Del costo clásico sólo hay anclas públicas —RSA de '
                      '829 bits— y proyecciones de papers. Defensa barata: biblioteca madura, OAEP y PSS.'),
           'agreements': [], 'differences': [], 'open_questions': []}
    question = '¿Qué componentes de RSA podrían ser vulnerados?'
    issues = synthesis.style_issues(rsa, conceptual=True, question=question)
    assert any('telegráfico' in i for i in issues)
    siglas = next(i for i in issues if 'Siglas' in i)
    assert 'OAEP' in siglas and 'PSS' in siglas and 'RSA' not in siglas, 'RSA viene en la pregunta'
    assert any('salvedades' in i for i in issues)
    ok = {'answer': ('RSA se rompe casi siempre por su entorno y no por su matemática. El relleno OAEP (el formato '
                     'moderno de empaquetar el mensaje) evita los ataques al formato antiguo. Sin los factores '
                     'primos, recuperar la clave equivale a factorizar el módulo. Las cifras para 2048 bits son '
                     'extrapolaciones, no mediciones.'), 'agreements': [], 'differences': [], 'open_questions': []}
    assert synthesis.style_issues(ok, conceptual=True, question=question) == []


# ------------------------------------------------------------ corrector de estilo

def test_el_corrector_de_estilo_reescribe_sin_cambiar_el_contenido():
    """El filtro de salida que pidió el operador («no se entiende»): una pasada
    fija de corrección antes de la revisión. Si el corrector pierde o inventa
    elementos, se descarta y queda el borrador."""
    telegram = dict(DRAFT, answer='Riesgo: entorno; no álgebra. Defensa: biblioteca madura, OAEP.',
                    blocking_conditions=['Agregar OAEP'], next_move=None)
    clear = dict(telegram, answer='El riesgo está en el entorno y no en la matemática. La defensa es una '
                 'biblioteca madura con relleno OAEP (formato moderno del mensaje).',
                 blocking_conditions=[], next_move={'title': 'inventado', 'reason': 'r', 'expected_result': 'e',
                                                    'scope': 's', 'risk': 'r', 'recommendation': 'discuss'})
    seen = []

    def invoke(seat, prompt):
        seen.append(prompt)
        return json.dumps(clear)
    out = synthesis.polish(telegram, {'seat': 'casper'}, invoke, question='¿Qué riesgo tiene RSA?', conceptual=False)
    assert out['polished'] is True and out['answer'] == clear['answer']
    assert out['blocking_conditions'] == ['Agregar OAEP'], 'las condiciones no las toca el corrector'
    assert out['next_move'] is None, 'ni inventa un siguiente movimiento'
    assert '¿Qué riesgo tiene RSA?' in seen[0] and 'no es especialista' in seen[0]
    # pierde una lista -> se descarta
    lost = dict(clear, differences=[])
    out = synthesis.polish(telegram, {'seat': 'casper'}, lambda s, p: json.dumps(lost), question='q')
    assert out['polished'] is False and out['answer'] == telegram['answer']
    # el corrector falla -> se descarta

    def broken(seat, prompt):
        raise TimeoutError()
    assert synthesis.polish(telegram, {'seat': 'casper'}, broken)['polished'] is False


def test_compose_pule_cada_borrador_antes_de_revisarlo():
    phases = []

    def invoke(seat, prompt):
        if 'corrector de estilo' in prompt:
            return json.dumps(dict(DRAFT, answer='Pulido.'))
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        assert '"Pulido."' in prompt, 'los revisores leen el texto pulido'
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke, progress=lambda r: phases.append(r.get('phase')))
    assert result['answer'] == 'Pulido.' and result['polished'] is True
    assert phases == ['drafting', 'polishing', 'reviewing']


def test_el_corrector_hace_una_segunda_pasada_si_alargo_o_rompio_reglas():
    """La primera versión pulida de #34 pasó de 150 a 244 palabras explicando
    «bits» y «biblioteca»; una segunda pasada con las reglas rotas la trae de
    vuelta. Si vuelve a fallar, se queda: legible pero larga es mejor que
    telegráfica."""
    long_answer = 'Dato ' * 300
    calls = []

    def invoke(seat, prompt):
        calls.append(prompt)
        if 'corrector de estilo' in prompt and 'rompía estas reglas' in prompt:
            return json.dumps(dict(DRAFT, answer='Corta y clara.'))
        if 'corrector de estilo' in prompt:
            return json.dumps(dict(DRAFT, answer=long_answer))
        if 'Redactá una respuesta' in prompt:
            return json.dumps(DRAFT)
        return json.dumps({'approve': True, 'feedback': ''})
    result = synthesis.compose(BUNDLE, SEATS, invoke)
    assert result['answer'] == 'Corta y clara.' and result['style_issues'] == []
    assert sum('corrector de estilo' in p for p in calls) == 2
    assert 'no puede superar las' in calls[1]
    assert synthesis.style_issues({'answer': 'La clave queda copiada en registros del sistema.',
                                   'agreements': [], 'differences': [], 'open_questions': []}, True) == []


def test_prosa_entrecortada_se_detecta():
    choppy = {'answer': ('RSA falla por su entorno. Fallan el azar y la custodia. Calcular el inverso no es un ataque. '
                         'Con los factores sale al instante. Sin ellos hay que factorizar. Nadie fijó una cifra.'),
              'agreements': [], 'differences': [], 'open_questions': []}
    assert any('entrecortada' in i for i in synthesis.style_issues(choppy, True, question='¿Cómo se rompe RSA?'))
    flowing = {'answer': ('RSA falla por su entorno y no por su matemática, porque los problemas reales aparecen '
                          'al generar las claves con poco azar o al guardar mal la clave privada. Calcular el '
                          'inverso de la clave privada no es un ataque en sí, ya que con los factores primos es '
                          'inmediato y sin ellos equivale a factorizar el módulo. Para claves de 2048 bits nadie '
                          'ha fijado una cifra de cómputo, así que sólo existen extrapolaciones.'),
               'agreements': [], 'differences': [], 'open_questions': []}
    assert synthesis.style_issues(flowing, True, question='¿Cómo se rompe RSA?') == []
