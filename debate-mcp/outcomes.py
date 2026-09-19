"""Human outcome reports with provenance; never automatic truth labels."""
import uuid


def record(conn, payload):
    if not isinstance(payload, dict):
        raise ValueError('Formato de resultado inválido')
    identifier = payload.get('decision_id')
    if type(identifier) is not int or identifier < 1:
        raise ValueError('Seleccioná una decisión')
    status = payload.get('status')
    if status not in ('worked','failed','partial','unknown'):
        raise ValueError('Resultado inválido')
    # Calificación rápida (un clic al abrir la siguiente decisión): vale más
    # un resultado sin detalle que ninguno — hasta el 2026-09-18 había cero
    # outcomes registrados porque el formulario pedía dos textos.
    quick = payload.get('quick') is True
    values = {}
    for key, limit in [('observation',2000),('evidence',2000),('lesson',1000)]:
        value = payload.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError('Describí lo observado y la evidencia (máximo 2000 caracteres; aprendizaje 1000)')
        if key != 'lesson' and not value.strip():
            if not quick:
                raise ValueError('Describí lo observado y la evidencia (máximo 2000 caracteres; aprendizaje 1000)')
            value = 'calificación rápida, sin detalle'
        values[key] = value.strip()
    try:
        request_id = str(uuid.UUID(payload.get('request_id', '')))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Identificador del reporte inválido') from None
    d = conn.execute('SELECT id,thread,status FROM decisions WHERE id=%s FOR UPDATE', (identifier,)).fetchone()
    if not d:
        raise ValueError('La decisión no existe')
    previous = conn.execute('SELECT * FROM decision_outcomes WHERE request_id=%s', (request_id,)).fetchone()
    if previous:
        if previous['decision_id'] != identifier or previous['status'] != status or any(previous[k] != v for k,v in values.items()):
            raise ValueError('Este envío ya fue usado para otro resultado')
        return {'id': previous['id'], 'decision_id': identifier}
    if d['status'] not in ('closed','executing','split'):
        raise ValueError('Esperá a que termine la deliberación para registrar el resultado')
    text = (f"Resultado reportado por el usuario: {status}. No es verificación independiente.\n"
            f"Observación: {values['observation']}\nEvidencia indicada: {values['evidence']}\n"
            f"Aprendizaje propuesto: {values['lesson'] or 'Sin conclusión todavía'}")
    message = conn.execute("INSERT INTO messages (thread,author,kind,body,artifact) VALUES (%s,'adrian','contexto',%s,NULL) RETURNING id",
                           (d['thread'],text)).fetchone()
    row = conn.execute('''INSERT INTO decision_outcomes
        (decision_id,request_id,status,observation,evidence,lesson,message_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (identifier,request_id,status,values['observation'],values['evidence'],values['lesson'],message['id'])).fetchone()
    return {'id': row['id'], 'decision_id': identifier}
