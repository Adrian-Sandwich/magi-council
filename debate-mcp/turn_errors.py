"""Persist failed decision turns without inventing votes or retrying silently."""
from uuid import uuid4

from psycopg.types.json import Jsonb


def active(d):
    if d.get('status') != 'open':
        return {}
    return {seat: failure for seat, failure in
            ((d.get('minority_report') or {}).get('turn_errors') or {}).items()
            if failure.get('round') == d['round']}


def record(conn, decision_id, seat, round_, reason):
    """Caller owns the transaction; ignore late completion and existing votes."""
    d = conn.execute('SELECT * FROM decisions WHERE id=%s FOR UPDATE', (decision_id,)).fetchone()
    if not d or d['status'] != 'open' or d['round'] != round_ or seat not in d['heads']:
        return False
    voted = conn.execute('SELECT 1 FROM positions WHERE decision_id=%s AND head=%s AND round=%s',
                         (decision_id, seat, round_)).fetchone()
    if voted or seat in active(d):
        return False
    errors = active(d)
    errors[seat] = {'id': uuid4().hex, 'round': round_, 'message': reason}
    conn.execute("""UPDATE decisions SET minority_report=COALESCE(minority_report,'{}'::jsonb)
        || %s::jsonb WHERE id=%s""", (Jsonb({'turn_errors': errors}), decision_id))
    conn.execute("""INSERT INTO messages (thread,author,kind,body,artifact)
        VALUES (%s,'magi','resultado',%s,NULL)""",
        (d['thread'], f"ERROR EN TURNO de {seat}: {reason} No se reintentará automáticamente. "
         'Corrige la causa y usa Reintentar cabezas fallidas. Los votos recibidos se conservan.'))
    # Con el error anotado, el motor puede cerrar la ronda si los que sí
    # votaron ya son mayoría y coinciden (import local: board no importa
    # este módulo, pero lo carga la UI antes que a board).
    from board import settle_degraded
    settle_degraded(conn, decision_id)
    return True


def retry(conn, decision_id, expected):
    d = conn.execute('SELECT * FROM decisions WHERE id=%s FOR UPDATE', (decision_id,)).fetchone()
    errors = active(d) if d else {}
    if not errors or {seat: error['id'] for seat, error in errors.items()} != expected:
        raise ValueError('Los errores de esta decisión cambiaron. Actualiza la vista antes de reintentar.')
    conn.execute("""UPDATE decisions SET minority_report=minority_report - 'turn_errors'
        WHERE id=%s""", (decision_id,))
    # Human intent resets the relay budget, without clearing any positions.
    conn.execute("""INSERT INTO messages (thread,author,kind,body,artifact)
        VALUES (%s,'adrian','analisis',%s,NULL)""",
        (d['thread'], 'Reintento solicitado para las cabezas fallidas: ' + ', '.join(errors)))
    return {'decision_id': decision_id, 'action': 'retried'}
