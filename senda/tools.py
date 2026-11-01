"""Herramientas del agente (formato tools de Chat Completions de Meta Model API).

Cada tool valida sus argumentos (strict=false en la API: el modelo puede mandar basura)
y devuelve JSON serializable. Las tools nunca tocan el consentimiento.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from . import calendar_service, clock, db, memory, payments, scheduler
from .config import get_business

log = logging.getLogger(__name__)

STAGES = ["frio", "caliente", "objecion", "pago_pendiente", "cliente", "perdido"]


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


def schemas() -> list[dict]:
    service_ids = [s["id"] for s in get_business()["services"]]
    return [
        _fn("get_available_slots", "Devuelve huecos libres reales para un servicio. Úsala SIEMPRE antes de proponer horas.",
            {"service_id": {"type": "string", "enum": service_ids},
             "date_from": {"type": "string", "description": "YYYY-MM-DD (opcional; por defecto hoy)"},
             "days": {"type": "integer", "description": "Días a mirar (1-14)"}},
            ["service_id"]),
        _fn("book_appointment", "Reserva una cita en un hueco devuelto por get_available_slots, tras confirmación explícita del cliente.",
            {"service_id": {"type": "string", "enum": service_ids},
             "start_iso": {"type": "string", "description": "Inicio en ISO 8601 con zona, exactamente como lo devolvió get_available_slots"},
             "patient_name": {"type": "string"}},
            ["service_id", "start_iso"]),
        _fn("cancel_appointment", "Cancela una cita del contacto.",
            {"appointment_id": {"type": "integer"}}, ["appointment_id"]),
        _fn("create_payment_link", "Genera enlace de pago Bizum para la señal de una cita. Solo si el servicio tiene señal > 0.",
            {"concept": {"type": "string"}, "amount_eur": {"type": "number"}, "appointment_id": {"type": "integer"}},
            ["concept", "amount_eur"]),
        _fn("remember", "Guarda un dato útil y NO sensible del cliente para personalizar el seguimiento (memoria de 30 días). "
            "Nunca guardes datos de salud.",
            {"key": {"type": "string", "enum": list(memory.ALLOWED_FACT_KEYS)}, "value": {"type": "string"}},
            ["key", "value"]),
        _fn("set_lead_stage", "Clasifica el lead tras cada mensaje del cliente.",
            {"stage": {"type": "string", "enum": STAGES}, "reason": {"type": "string"}}, ["stage", "reason"]),
        _fn("start_followup", "Activa una secuencia de seguimiento. 'sin_respuesta' si preguntó y no cerró; "
            "'presupuesto_sin_cita' si le diste precio y no reservó; 'pago_pendiente' si reservó y falta la señal.",
            {"sequence": {"type": "string", "enum": ["sin_respuesta", "presupuesto_sin_cita", "pago_pendiente"]},
             "note": {"type": "string", "description": "Contexto para personalizar el seguimiento"}},
            ["sequence"]),
        _fn("schedule_followup_at", "Programa un seguimiento en una fecha concreta acordada con el cliente "
            "(p. ej. 'cobro el 15', 'escríbeme el lunes').",
            {"datetime_iso": {"type": "string", "description": "ISO 8601 con zona Europe/Madrid"},
             "note": {"type": "string", "description": "Qué se acordó y qué hay que retomar"}},
            ["datetime_iso", "note"]),
        _fn("handoff_to_human", "Pasa la conversación a una persona del equipo (urgencias, quejas, temas clínicos, "
            "o si el cliente lo pide). Pausa los seguimientos automáticos.",
            {"reason": {"type": "string"}, "summary": {"type": "string", "description": "Resumen para el humano"}},
            ["reason", "summary"]),
    ]


def dispatch(contact: dict, name: str, args: dict) -> dict:
    cid = contact["id"]
    try:
        if name == "get_available_slots":
            days = max(1, min(int(args.get("days") or 5), 14))
            slots = calendar_service.available_slots(args["service_id"], args.get("date_from"), days=days)
            return {"slots": slots, "zona": "Europe/Madrid"} if slots else {"slots": [], "nota": "Sin huecos en ese rango"}
        if name == "book_appointment":
            res = calendar_service.book(cid, args["service_id"], args["start_iso"], args.get("patient_name"))
            if res.get("ok"):
                scheduler.schedule_appointment_reminder(cid, res["appointment_id"], datetime.fromisoformat(res["inicio"]))
                scheduler.cancel_on_inbound(cid)  # ya reservó: fuera seguimientos comerciales
                db.execute("UPDATE contacts SET stage='cliente' WHERE id=%s AND stage <> 'cliente'", (cid,))
                if res.get("senal_eur"):
                    res["siguiente_paso"] = "Generar enlace de pago de la señal con create_payment_link y activar pago_pendiente"
            return res
        if name == "cancel_appointment":
            return calendar_service.cancel(cid, int(args["appointment_id"]))
        if name == "create_payment_link":
            res = payments.create_link(cid, str(args["concept"])[:120], float(args["amount_eur"]), args.get("appointment_id"))
            if res.get("ok"):
                db.execute("UPDATE contacts SET stage='pago_pendiente' WHERE id=%s", (cid,))
            return res
        if name == "remember":
            return {"result": memory.remember_fact(cid, args["key"], str(args["value"]))}
        if name == "set_lead_stage":
            stage = args.get("stage")
            if stage not in STAGES:
                return {"error": f"stage debe ser uno de {STAGES}"}
            db.execute("UPDATE contacts SET stage=%s WHERE id=%s", (stage, cid))
            return {"ok": True}
        if name == "start_followup":
            if contact["consent_state"] != "granted":
                return {"ok": False, "nota": "Sin consentimiento de seguimiento: no se programa nada (RGPD). No insistas."}
            rows = scheduler.schedule_sequence(cid, args["sequence"], args.get("note"))
            return {"ok": True, "programados": [r["due_at"].isoformat() for r in rows]}
        if name == "schedule_followup_at":
            if contact["consent_state"] != "granted":
                return {"ok": False, "nota": "Sin consentimiento de seguimiento: no se programa nada (RGPD)."}
            due = datetime.fromisoformat(args["datetime_iso"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=clock.tz())
            row = scheduler.schedule_at(cid, due, args["note"])
            return {"ok": True, "programado": row["due_at"].isoformat()}
        if name == "handoff_to_human":
            with db.tx() as c:
                c.execute("INSERT INTO handoffs(contact_id, reason, summary, created_at) VALUES (%s,%s,%s,%s)",
                          (cid, args["reason"], args["summary"], clock.now()))
                c.execute("UPDATE contacts SET status='handoff' WHERE id=%s", (cid,))
            return {"ok": True, "nota": "Equipo avisado. Dile al cliente que una persona le escribe en breve."}
        return {"error": f"Tool desconocida: {name}"}
    except KeyError as e:
        return {"error": f"Falta el argumento {e}"}
    except (ValueError, TypeError) as e:
        return {"error": f"Argumento inválido: {e}"}


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
