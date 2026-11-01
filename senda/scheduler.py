"""Motor de seguimiento con tiempos humanos.

- Secuencias declarativas (prompts/followups.json) que el agente activa por etapa del lead.
- Política dura, evaluada en el momento de enviar (no al programar): consentimiento vigente,
  no estar en manos de un humano, horario permitido (09-21 h Europe/Madrid, sin domingos),
  tope de mensajes de seguimiento por contacto.
- Cualquier respuesta del cliente cancela los seguimientos comerciales pendientes:
  ese mensaje que ya no se envía es dinero ahorrado (se cobra por mensaje fuera de ventana).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

from . import clock, db, gdpr
from .config import get_settings

log = logging.getLogger(__name__)
MIN_GAP = timedelta(hours=6)
SALES_SEQUENCES = ("sin_respuesta", "presupuesto_sin_cita", "personalizado")


@lru_cache
def sequences() -> dict:
    return json.loads((Path(__file__).parent / "prompts" / "followups.json").read_text(encoding="utf-8"))


def adjust_to_allowed(dt: datetime) -> datetime:
    """Desplaza dt a la siguiente franja permitida en hora local."""
    s = get_settings()
    tz = clock.tz()
    local = dt.astimezone(tz)
    for _ in range(10):
        if s.no_followups_on_sunday and local.weekday() == 6:
            local = datetime.combine(local.date() + timedelta(days=1), time(s.quiet_hours_end, 30), tz)
            continue
        if local.hour >= s.quiet_hours_start:
            local = datetime.combine(local.date() + timedelta(days=1), time(s.quiet_hours_end, 30), tz)
            continue
        if local.hour < s.quiet_hours_end:
            local = datetime.combine(local.date(), time(s.quiet_hours_end, 30), tz)
            continue
        break
    return local


def schedule_sequence(contact_id: int, sequence: str, note: str | None = None, base: datetime | None = None) -> list[dict]:
    seq = sequences().get(sequence)
    if not seq or sequence == "recordatorio_cita":
        raise ValueError(f"Secuencia no válida: {sequence}")
    base = base or clock.now()
    now = clock.now()
    created = []
    with db.tx() as c:
        if sequence in SALES_SEQUENCES or sequence == "pago_pendiente":
            # una sola secuencia comercial activa por contacto: la nueva sustituye a la anterior
            c.execute(
                "UPDATE followups SET status='cancelled', outcome=%s, executed_at=%s "
                "WHERE contact_id=%s AND status='scheduled' AND sequence = ANY(%s)",
                (f"sustituida por {sequence}", now, contact_id,
                 list(SALES_SEQUENCES) + (["pago_pendiente"] if sequence == "pago_pendiente" else [])),
            )
        for i, step in enumerate(seq["steps"], start=1):
            due = adjust_to_allowed(base + timedelta(hours=step["after_hours"]))
            row = c.execute(
                "INSERT INTO followups(contact_id, sequence, step, reason, note, due_at, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, sequence, step, due_at",
                (contact_id, sequence, i, step["goal"], note, due, now),
            ).fetchone()
            created.append(row)
    return created


def schedule_at(contact_id: int, due: datetime, note: str) -> dict:
    due = adjust_to_allowed(max(due, clock.now() + timedelta(minutes=5)))
    with db.tx() as c:
        c.execute(
            "UPDATE followups SET status='cancelled', outcome='sustituida por personalizado', executed_at=%s "
            "WHERE contact_id=%s AND status='scheduled' AND sequence = ANY(%s)",
            (clock.now(), contact_id, list(SALES_SEQUENCES)),
        )
        return c.execute(
            "INSERT INTO followups(contact_id, sequence, step, reason, note, due_at, created_at) "
            "VALUES (%s,'personalizado',1,%s,%s,%s,%s) RETURNING id, sequence, step, due_at",
            (contact_id, sequences()["personalizado"]["steps"][0]["goal"], note, due, clock.now()),
        ).fetchone()


def schedule_appointment_reminder(contact_id: int, appointment_id: int, start: datetime) -> dict | None:
    hours = sequences()["recordatorio_cita"]["steps"][0]["before_appointment_hours"]
    due = start - timedelta(hours=hours)
    if due <= clock.now():
        return None
    adjusted = adjust_to_allowed(due)
    if adjusted >= start - timedelta(hours=2):
        # cae en horario no permitido justo antes de la cita: retrocede al último día permitido a las 12:00
        tz = clock.tz()
        d = due.astimezone(tz).date()
        for _ in range(3):
            d -= timedelta(days=1)
            cand = datetime.combine(d, time(12, 0), tz)
            if adjust_to_allowed(cand) == cand:
                break
        adjusted = cand
    due = adjusted
    if due <= clock.now() or due >= start:
        return None
    return db.fetchone(
        "INSERT INTO followups(contact_id, sequence, step, reason, note, due_at, created_at) "
        "VALUES (%s,'recordatorio_cita',1,%s,%s,%s,%s) RETURNING id, due_at",
        (contact_id, sequences()["recordatorio_cita"]["steps"][0]["goal"], f"appointment_id={appointment_id}",
         due, clock.now()),
    )


def cancel_on_inbound(contact_id: int) -> int:
    with db.tx() as c:
        cur = c.execute(
            "UPDATE followups SET status='cancelled', outcome='el cliente respondió (mensaje evitado)', executed_at=%s "
            "WHERE contact_id=%s AND status='scheduled' AND sequence = ANY(%s)",
            (clock.now(), contact_id, list(SALES_SEQUENCES)),
        )
        return cur.rowcount


def pending_for(contact_id: int) -> list[dict]:
    return db.fetchall(
        "SELECT id, sequence, step, due_at, note FROM followups WHERE contact_id=%s AND status='scheduled' ORDER BY due_at",
        (contact_id,),
    )


def _sent_count(contact_id: int) -> int:
    row = db.fetchone(
        "SELECT count(*) AS n FROM followups WHERE contact_id=%s AND status='sent' AND sequence = ANY(%s) "
        "AND executed_at > %s",
        (contact_id, list(SALES_SEQUENCES), clock.now() - timedelta(days=30)),
    )
    return row["n"]


def run_due(limit: int = 20) -> list[dict]:
    """Ejecuta los seguimientos vencidos. Seguro con varios workers (SKIP LOCKED)."""
    from . import agent  # import diferido (evita ciclo)
    from .channels import outbound

    results = []
    now = clock.now()
    with db.tx() as c:
        due = c.execute(
            "SELECT * FROM followups WHERE status='scheduled' AND due_at <= %s ORDER BY due_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (now, limit),
        ).fetchall()
        claimed = [f["id"] for f in due]
        if claimed:
            c.execute("UPDATE followups SET status='running' WHERE id = ANY(%s)", (claimed,))

    for f in due:
        outcome, status, msg_id = None, "skipped", None
        try:
            contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (f["contact_id"],))
            ok, why = gdpr.can_contact_proactively(contact)
            if not ok:
                outcome = f"bloqueado por política: {why}"
            elif f["sequence"] in SALES_SEQUENCES and _sent_count(contact["id"]) >= get_settings().max_followups_per_contact:
                outcome = "tope de seguimientos alcanzado"
            elif f["sequence"] == "pago_pendiente" and not db.fetchone(
                "SELECT 1 FROM payments WHERE contact_id=%s AND status='pending'", (contact["id"],)
            ):
                outcome = "ya pagó (mensaje evitado)"
            elif f["sequence"] == "pago_pendiente" and not db.fetchone(
                "SELECT 1 FROM appointments WHERE contact_id=%s AND status='confirmed' AND start_at > %s",
                (contact["id"], now),
            ):
                outcome = "sin cita futura: no procede reclamar la señal"
            elif f["sequence"] == "recordatorio_cita" and not _appointment_active(f):
                outcome = "cita cancelada (mensaje evitado)"
            else:
                allowed = adjust_to_allowed(now)
                recent = db.fetchone(
                    "SELECT max(executed_at) AS t FROM followups WHERE contact_id=%s AND status='sent'", (contact["id"],)
                )["t"]
                if recent and now - recent < MIN_GAP:
                    # nunca dos seguimientos seguidos al mismo cliente (p. ej. tras una caída del worker)
                    allowed = adjust_to_allowed(recent + MIN_GAP)
                if allowed > now + timedelta(minutes=1):
                    db.execute("UPDATE followups SET status='scheduled', due_at=%s WHERE id=%s", (allowed, f["id"]))
                    results.append({"id": f["id"], "status": "rescheduled", "due_at": allowed})
                    continue
                text = agent.compose_followup(contact, f)
                origin = "reminder" if f["sequence"] == "recordatorio_cita" else "followup"
                sent = outbound.send(contact, text, origin=origin, category=sequences()[f["sequence"]]["category"])
                status, msg_id, outcome = "sent", sent["id"], f"{sent['category']} · {sent['cost_eur']:.4f} €"
        except Exception as e:  # noqa: BLE001
            log.exception("Seguimiento %s falló", f["id"])
            status, outcome = "scheduled", f"error: {e}"[:300]
            db.execute("UPDATE followups SET status='scheduled', due_at=%s, outcome=%s WHERE id=%s",
                       (now + timedelta(minutes=10), outcome, f["id"]))
            results.append({"id": f["id"], "status": "retry", "outcome": outcome})
            continue
        db.execute(
            "UPDATE followups SET status=%s, outcome=%s, message_id=%s, executed_at=%s WHERE id=%s",
            (status, outcome, msg_id, clock.now(), f["id"]),
        )
        results.append({"id": f["id"], "status": status, "outcome": outcome})
    return results


def _appointment_active(f: dict) -> bool:
    note = f.get("note") or ""
    if not note.startswith("appointment_id="):
        return True
    row = db.fetchone("SELECT status FROM appointments WHERE id=%s", (int(note.split("=")[1]),))
    return bool(row and row["status"] == "confirmed")
