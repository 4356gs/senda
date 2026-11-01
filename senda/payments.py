"""Pagos de señal.

IMPORTANTE (honestidad en la demo): Bizum no ofrece una API pública para generar enlaces de pago;
a comercios se llega vía TPV virtual (Redsys u otra pasarela del banco). Aquí `bizum_mock` genera un
enlace propio que simula la pasarela y dispara el mismo webhook de confirmación que tendría Redsys.
Sustituir `create_link` por la integración real de la pasarela del cliente es un cambio acotado.
"""
from __future__ import annotations

import secrets

from . import clock, db
from .config import get_settings


def create_link(contact_id: int, concept: str, amount_eur: float, appointment_id: int | None = None) -> dict:
    if amount_eur <= 0 or amount_eur > 2000:
        return {"ok": False, "error": "Importe fuera de rango"}
    existing = db.fetchone(
        "SELECT token, amount_eur FROM payments WHERE contact_id=%s AND status='pending' AND concept=%s",
        (contact_id, concept),
    )
    token = existing["token"] if existing else secrets.token_urlsafe(12)
    if not existing:
        db.execute(
            "INSERT INTO payments(contact_id, appointment_id, concept, amount_eur, provider, token, created_at) "
            "VALUES (%s,%s,%s,%s,'bizum_mock',%s,%s)",
            (contact_id, appointment_id, concept, amount_eur, token, clock.now()),
        )
    url = f"{get_settings().public_base_url.rstrip('/')}/pagar/{token}"
    return {"ok": True, "url": url, "importe_eur": float(amount_eur), "metodo": "Bizum (simulado en demo)"}


def mark_paid(token: str) -> dict | None:
    with db.tx() as c:
        row = c.execute(
            "UPDATE payments SET status='paid', paid_at=%s WHERE token=%s AND status='pending' RETURNING *",
            (clock.now(), token),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE followups SET status='cancelled', outcome='pago recibido', executed_at=%s "
                "WHERE contact_id=%s AND status='scheduled' AND sequence='pago_pendiente'",
                (clock.now(), row["contact_id"]),
            )
            c.execute("UPDATE contacts SET stage='cliente' WHERE id=%s", (row["contact_id"],))
    return row


def pending(contact_id: int) -> list[dict]:
    return db.fetchall(
        "SELECT concept, amount_eur, token, created_at FROM payments WHERE contact_id=%s AND status='pending'",
        (contact_id,),
    )
