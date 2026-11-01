"""Capa RGPD determinista.

Principio de diseño: el LLM NUNCA decide sobre consentimiento. Esta capa corre antes del agente,
con reglas auditables, y deja evidencia literal en consent_events (art. 7.1 RGPD: el responsable
debe poder demostrar el consentimiento).

- Responder a lo que el cliente pregunta = medidas precontractuales a petición del interesado (art. 6.1.b).
- Seguimiento proactivo y memoria de 30 días = consentimiento explícito (art. 6.1.a), revocable con BAJA.
"""
from __future__ import annotations

import re
import unicodedata

from . import clock, db
from .config import get_business, get_settings

PURPOSE_FOLLOWUP = "Seguimiento de tu consulta, recordatorios de cita y memoria de la conversación durante 30 días"
PURPOSE_SERVICE = "Responder a tu consulta (medidas precontractuales a petición del interesado)"


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


_OPT_OUT = re.compile(
    r"^(baja|stop|unsubscribe|cancelar suscripcion)[.! ]*$"
    r"|\b(dame de baja|darme de baja|dadme de baja|no me escrib\w*|dej\w* de escrib\w*|no quiero (recibir|que me escrib)\w*"
    r"|no me (mandes|mandeis|envies|envieis) mas|borra(d)? mis datos|elimina(d)? mis datos)\b"
)
_YES = re.compile(r"^(si+|sip|vale|ok|okay|okey|de acuerdo|claro|adelante|acepto|por supuesto|perfecto|dale|venga)\b")
_NO = re.compile(r"^(no|nop|mejor no|prefiero que no|no gracias|paso)\b")
_REGRANT = re.compile(r"^(alta|reactivar|darme de alta|quiero seguimiento)[.! ]*$")


def consent_request_text() -> str:
    b = get_business()
    return (
        f"Una cosa más 🙂 Para poder recordarte la cita y hacerte seguimiento durante los próximos 30 días, "
        f"necesito tu permiso. Guardamos solo lo necesario y puedes escribir BAJA cuando quieras.\n"
        f"Responde *SÍ* para aceptar o *NO* si prefieres que solo te conteste cuando escribas tú.\n"
        f"Info: {b.get('privacy_url', '')}"
    )


def opt_out_confirmation_text() -> str:
    return (
        "Hecho: te he dado de baja. No te enviaremos más mensajes de seguimiento ni recordatorios, "
        "y hemos borrado la memoria de la conversación. Si nos escribes, te responderemos igualmente. "
        "Para volver a activarlo, escribe ALTA."
    )


def evaluate_inbound(contact: dict, text: str | None) -> str | None:
    """Devuelve la acción de consentimiento que dispara el mensaje entrante, o None."""
    t = _norm(text or "")
    if not t:
        return None
    if _OPT_OUT.search(t):
        return "opt_out"
    state = contact["consent_state"]
    if state in ("revoked", "denied") and _REGRANT.match(t):
        return "regrant"
    if state == "requested":
        if _YES.match(t):
            return "granted"
        if _NO.match(t) and len(t) <= 40:
            return "denied"
    return None


def log_event(conn, contact_id: int, event: str, evidence: str | None, message_id: int | None = None,
              purpose: str = PURPOSE_FOLLOWUP, legal_basis: str = "consentimiento art. 6.1.a") -> None:
    conn.execute(
        "INSERT INTO consent_events(contact_id, event, purpose, legal_basis, evidence_text, message_id, policy_version, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (contact_id, event, purpose, legal_basis, (evidence or "")[:1000], message_id,
         get_settings().consent_policy_version, clock.now()),
    )


def apply_action(contact: dict, action: str, evidence: str, message_id: int | None) -> None:
    cid = contact["id"]
    with db.tx() as c:
        if action == "granted" or action == "regrant":
            c.execute("UPDATE contacts SET consent_state='granted', status='active' WHERE id=%s", (cid,))
            log_event(c, cid, "granted", evidence, message_id)
        elif action == "denied":
            c.execute("UPDATE contacts SET consent_state='denied' WHERE id=%s", (cid,))
            log_event(c, cid, "denied", evidence, message_id)
            _cancel_followups(c, cid, "consentimiento denegado")
        elif action == "opt_out":
            c.execute("UPDATE contacts SET consent_state='revoked', status='opted_out' WHERE id=%s", (cid,))
            log_event(c, cid, "revoked", evidence, message_id)
            _cancel_followups(c, cid, "BAJA (revocación)")
            # Minimización: al revocar se borra la memoria usada para seguimiento
            c.execute("DELETE FROM memory_items WHERE contact_id=%s", (cid,))
            c.execute("DELETE FROM contact_facts WHERE contact_id=%s", (cid,))


def mark_requested(contact_id: int, text: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE contacts SET consent_state='requested' WHERE id=%s AND consent_state='unknown'", (contact_id,))
        log_event(c, contact_id, "requested", text)


def _cancel_followups(conn, contact_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE followups SET status='cancelled', outcome=%s, executed_at=%s WHERE contact_id=%s AND status='scheduled'",
        (reason, clock.now(), contact_id),
    )


def can_contact_proactively(contact: dict) -> tuple[bool, str]:
    if contact["consent_state"] != "granted":
        return False, f"sin consentimiento ({contact['consent_state']})"
    if contact["status"] == "opted_out":
        return False, "baja"
    if contact["status"] == "handoff":
        return False, "en manos de un humano"
    if contact["status"] == "erased":
        return False, "datos borrados"
    return True, "ok"


# ---------- Derechos ARSOPL ----------

def export_contact(contact_id: int) -> dict:
    """Derecho de acceso / portabilidad (arts. 15 y 20)."""
    out = {
        "contacto": db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact_id,)),
        "consentimientos": db.fetchall("SELECT * FROM consent_events WHERE contact_id=%s ORDER BY created_at", (contact_id,)),
        "mensajes": db.fetchall(
            "SELECT direction, kind, body, transcript, created_at FROM messages WHERE contact_id=%s ORDER BY created_at",
            (contact_id,),
        ),
        "hechos": db.fetchall("SELECT key, value, updated_at FROM contact_facts WHERE contact_id=%s", (contact_id,)),
        "citas": db.fetchall("SELECT service, start_at, status FROM appointments WHERE contact_id=%s", (contact_id,)),
        "pagos": db.fetchall("SELECT concept, amount_eur, status, created_at FROM payments WHERE contact_id=%s", (contact_id,)),
    }
    with db.tx() as c:
        log_event(c, contact_id, "exported", "Exportación de datos (art. 15/20)", None,
                  purpose="Ejercicio de derechos", legal_basis="obligación legal art. 6.1.c")
    return out


def erase_contact(contact_id: int) -> None:
    """Derecho de supresión (art. 17). Conserva solo el rastro mínimo de consentimiento como prueba."""
    with db.tx() as c:
        for table in ("memory_items", "contact_facts", "messages", "followups"):
            c.execute(f"DELETE FROM {table} WHERE contact_id=%s", (contact_id,))  # noqa: S608 (tablas fijas)
        c.execute(
            "UPDATE contacts SET display_name=NULL, status='erased', consent_state='revoked', preferred_format=NULL WHERE id=%s",
            (contact_id,),
        )
        log_event(c, contact_id, "erased", "Supresión de datos (art. 17)", None,
                  purpose="Ejercicio de derechos", legal_basis="obligación legal art. 6.1.c")
