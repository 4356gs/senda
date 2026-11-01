"""Envío saliente con conciencia de la ventana de 24 h y del coste por mensaje de WhatsApp.

Reglas de la plataforma (modelo de precios por mensaje):
- Dentro de la ventana de atención (24 h desde el último mensaje del cliente): texto libre, sin coste;
  las plantillas utility también son gratis dentro de la ventana.
- Fuera de la ventana: solo plantillas aprobadas, que se cobran por categoría (marketing / utility).
El agente decide QUÉ decir; esta capa decide CÓMO se puede enviar y cuánto cuesta.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import clock, db, memory
from ..config import get_settings
from . import whatsapp

log = logging.getLogger(__name__)
WINDOW = timedelta(hours=24)

# origen -> categoría de plantilla si hay que salir de la ventana
_CATEGORY = {"followup": "marketing", "reminder": "utility", "payment": "utility", "agent": "utility",
             "gdpr": "utility", "human": "utility"}


def in_window(contact: dict) -> bool:
    last = contact.get("last_inbound_at")
    return bool(last and clock.now() - last < WINDOW)


def price_for(category: str) -> float:
    s = get_settings()
    return {"marketing": s.price_marketing_eur, "utility": s.price_utility_eur}.get(category, 0.0)


def plan(contact: dict, origin: str, category: str | None = None) -> tuple[str, float, bool]:
    """Devuelve (pricing_category, coste_eur, usa_plantilla)."""
    if in_window(contact):
        return "service", 0.0, False
    cat = category or _CATEGORY.get(origin, "marketing")
    return cat, price_for(cat), True


def send(contact: dict, text: str, origin: str = "agent", category: str | None = None) -> dict:
    s = get_settings()
    category, cost, use_template = plan(contact, origin, category)
    wa_msg_id = None
    kind = "template" if use_template else "text"
    if contact["channel"] == "whatsapp" and s.whatsapp_enabled:
        try:
            if use_template:
                tpl = s.wa_template_followup if category == "marketing" else s.wa_template_reminder
                name = (contact.get("display_name") or "").split(" ")[0] or "hola"
                wa_msg_id = whatsapp.send_template(contact["wa_id"], tpl, [name, text])
            else:
                wa_msg_id = whatsapp.send_text(contact["wa_id"], text)
        except Exception as e:  # noqa: BLE001
            log.exception("Fallo enviando a WhatsApp")
            raise RuntimeError(f"WhatsApp send failed: {e}") from e
    now = clock.now()
    with db.tx() as c:
        row = c.execute(
            "INSERT INTO messages(contact_id, direction, kind, body, wa_message_id, origin, pricing_category, cost_eur, created_at) "
            "VALUES (%s,'out',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (contact["id"], kind, text, wa_msg_id, origin, category, cost, now),
        ).fetchone()
        c.execute("UPDATE contacts SET last_outbound_at=%s WHERE id=%s", (now, contact["id"]))
    if origin in ("agent", "followup"):
        memory.add_memory(contact["id"], "message", f"[{now:%Y-%m-%d}] Clínica: {text}")
    return {"id": row["id"], "category": category, "cost_eur": cost, "template": use_template}
