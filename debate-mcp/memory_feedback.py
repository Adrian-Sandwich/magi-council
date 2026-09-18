"""Etiquetas humanas sobre la memoria mostrada al consejo; nunca automáticas.

Cada decisión guarda en `minority_report.memory_sources` qué nodos del grafo
se inyectaron en los prompts de la ronda. El operador califica esa memoria
(¿sirvió?) y la etiqueta queda con la foto de las fuentes: es el conjunto
calificado a mano que la evaluación de recuperación no tenía.
"""
from psycopg.types.json import Json

NOTE_LIMIT = 500


def record(conn, payload):
    """Caller owns the transaction. Devuelve {'id', 'decision_id', 'useful'}."""
    if not isinstance(payload, dict):
        raise ValueError('Formato de calificación inválido')
    identifier = payload.get('decision_id')
    if type(identifier) is not int or identifier < 1:
        raise ValueError('Seleccioná una decisión')
    useful = payload.get('useful')
    if type(useful) is not bool:
        raise ValueError('Indicá si la memoria sirvió o no')
    note = payload.get('note', '')
    if not isinstance(note, str) or len(note.strip()) > NOTE_LIMIT:
        raise ValueError(f'La nota tiene un máximo de {NOTE_LIMIT} caracteres')
    note = note.strip()
    d = conn.execute('SELECT id, thread, round, minority_report FROM decisions WHERE id=%s FOR UPDATE',
                     (identifier,)).fetchone()
    if not d:
        raise ValueError('La decisión no existe')
    shown = (d.get('minority_report') or {}).get('memory_sources') or {}
    sources = list(shown.get('ids') or [])
    if not sources:
        raise ValueError('Esta decisión no consultó memoria del grafo')
    text = (f"Memoria calificada por el operador: {'sirvió' if useful else 'no sirvió'}. "
            f"Fuentes: {', '.join(sources)}." + (f"\nNota: {note}" if note else ''))
    # El mensaje deja rastro en el journal (y dispara el refresco de la UI y
    # la sincronización del grafo); no reabre nada ni cambia votos.
    message = conn.execute(
        "INSERT INTO messages (thread,author,kind,body,artifact) VALUES (%s,'adrian','contexto',%s,NULL) RETURNING id",
        (d['thread'], text)).fetchone()
    row = conn.execute(
        '''INSERT INTO memory_feedback (decision_id, round, useful, note, sources, message_id)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
        (identifier, shown.get('round') or d['round'], useful, note, Json(sources), message['id'])).fetchone()
    return {'id': row['id'], 'decision_id': identifier, 'useful': useful}
