"""Orquestador: mensaje entrante -> capa RGPD -> agente con tools (Muse Spark) -> respuesta.

Pipeline de un mensaje entrante:
  1. Alta/actualización del contacto y deduplicación por wa_message_id.
  2. Normalización multimodal: audio -> WAV 16 kHz -> Muse Voice Transcribe; imagen -> data URL para Muse Spark.
  3. Cualquier respuesta cancela los seguimientos comerciales pendientes (mensajes evitados).
  4. Capa RGPD determinista (BAJA / SÍ / NO / ALTA). El LLM no decide sobre consentimiento.
  5. Agente con memoria (hechos + pgvector) y tools; bucle de tool calling hasta respuesta final.
  6. Envío según ventana de 24 h; primer contacto -> petición de consentimiento.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import calendar_service, clock, db, gdpr, memory, payments, scheduler, tools
from .channels import outbound
from .config import get_business, get_settings
from .llm import get_llm, to_wav_16k_mono

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 6
HISTORY = 14
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_SYSTEM = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- contexto

def _fmt_hours(h: dict) -> str:
    names = {"mon": "L", "tue": "M", "wed": "X", "thu": "J", "fri": "V", "sat": "S", "sun": "D"}
    parts = []
    for k, v in h.items():
        parts.append(f"{names[k]} " + (" y ".join(f"{v[i]}-{v[i+1]}" for i in range(0, len(v), 2)) if v else "cerrado"))
    return ", ".join(parts)


def build_system(contact: dict, query: str, turn_note: str = "") -> str:
    b = get_business()
    facts = memory.facts(contact["id"])
    recalled = memory.recall(contact["id"], query) if query else []
    appts = calendar_service.upcoming(contact["id"])
    pays = payments.pending(contact["id"])
    fups = scheduler.pending_for(contact["id"])
    tz = clock.tz()
    return _SYSTEM.format(
        agent_name=b["agent_name"], business_name=b["name"], city=b["city"], tone=b["tone"], address=b["address"],
        opening_hours=_fmt_hours(b["opening_hours"]),
        services="\n".join(f"  - {s['id']} · {s['name']} · {s['duration_min']} min · "
                           f"{'gratis' if not s['price_eur'] else str(s['price_eur']) + ' €'} · "
                           f"señal {s.get('deposit_eur', 0)} €" for s in b["services"]),
        payment=f"{b['payment']['method']}. {b['payment']['note']}",
        faq="\n".join(f"  - {x}" for x in b.get("faq", [])),
        escalation="\n".join(f"  - {x}" for x in b.get("escalation_rules", [])),
        now_local=clock.local_now().strftime("%A %d/%m/%Y %H:%M"),
        consent_state=contact["consent_state"], stage=contact["stage"],
        facts=json.dumps(facts, ensure_ascii=False) if facts else "ninguno",
        appointments="; ".join(f"#{a['id']} {a['service']} {a['start_at'].astimezone(tz):%a %d/%m %H:%M}" for a in appts) or "ninguna",
        payments="; ".join(f"{p['concept']} {p['amount_eur']} € -> {get_settings().public_base_url}/pagar/{p['token']}" for p in pays) or "ninguno",
        followups="; ".join(f"{f['sequence']}#{f['step']} {f['due_at'].astimezone(tz):%d/%m %H:%M}" for f in fups) or "ninguno",
        recall="\n".join(f"  - ({r['created_at']:%d/%m}) {r['content']}" for r in recalled) or "  - (nada relevante)",
        turn_note=f"\n## Nota del sistema para este turno\n{turn_note}" if turn_note else "",
    )


def history(contact_id: int, exclude_id: int | None = None, n: int = HISTORY) -> list[dict]:
    rows = db.fetchall(
        "SELECT id, direction, kind, body, transcript FROM messages WHERE contact_id=%s AND id <> %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (contact_id, exclude_id or 0, n),
    )
    out = []
    for r in reversed(rows):
        if r["direction"] == "in":
            text = r["transcript"] and f"[nota de voz] {r['transcript']}" or r["body"] or f"[{r['kind']}]"
            out.append({"role": "user", "content": text})
        else:
            out.append({"role": "assistant", "content": r["body"] or ""})
    # la API exige alternancia razonable: fusiona mensajes consecutivos del mismo rol
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(m)
    return merged


# ---------------------------------------------------------------- entrada

def upsert_contact(wa_id: str, name: str | None, channel: str) -> dict:
    with db.tx() as c:
        row = c.execute(
            "INSERT INTO contacts(wa_id, display_name, channel, created_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (wa_id) DO UPDATE SET display_name = COALESCE(contacts.display_name, EXCLUDED.display_name) "
            "RETURNING *",
            (wa_id, name, channel, clock.now()),
        ).fetchone()
    return row


def handle_inbound(item: dict) -> dict:
    """item: {wa_id, name, channel, wa_message_id, kind, text, audio_bytes, image_bytes, image_mime}"""
    with _locks[item["wa_id"]]:
        return _handle(item)


def _handle(item: dict) -> dict:
    contact = upsert_contact(item["wa_id"], item.get("name"), item.get("channel", "whatsapp"))
    if contact["status"] == "erased":
        db.execute("UPDATE contacts SET status='active', consent_state='unknown' WHERE id=%s", (contact["id"],))
        contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
    if item.get("wa_message_id") and db.fetchone("SELECT 1 FROM messages WHERE wa_message_id=%s", (item["wa_message_id"],)):
        return {"status": "duplicate"}

    llm = get_llm()
    transcript, image_url = None, None
    if item.get("audio_bytes"):
        try:
            transcript = llm.transcribe(to_wav_16k_mono(item["audio_bytes"]),
                                        keywords=[s["name"] for s in get_business()["services"]])
        except Exception as e:  # noqa: BLE001
            log.exception("Transcripción fallida")
            transcript = f"(no se pudo transcribir el audio: {e.__class__.__name__})"
    if item.get("image_bytes"):
        mime = item.get("image_mime") or "image/jpeg"
        image_url = f"data:{mime};base64,{base64.b64encode(item['image_bytes']).decode()}"

    now = clock.now()
    with db.tx() as c:
        msg = c.execute(
            "INSERT INTO messages(contact_id, direction, kind, body, transcript, media_ref, wa_message_id, created_at) "
            "VALUES (%s,'in',%s,%s,%s,%s,%s,%s) RETURNING id",
            (contact["id"], item.get("kind", "text"), item.get("text"), transcript,
             item.get("media_ref"), item.get("wa_message_id"), now),
        ).fetchone()
        c.execute("UPDATE contacts SET last_inbound_at=%s WHERE id=%s", (now, contact["id"]))
    contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
    user_text = transcript or item.get("text") or ""
    avoided = scheduler.cancel_on_inbound(contact["id"])

    # --- Capa RGPD determinista
    action = gdpr.evaluate_inbound(contact, user_text)
    if action == "opt_out":
        gdpr.apply_action(contact, action, user_text, msg["id"])
        contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
        outbound.send(contact, gdpr.opt_out_confirmation_text(), origin="gdpr")
        return {"status": "opted_out", "avoided": avoided}
    turn_note = ""
    first_contact = contact["consent_state"] == "unknown"
    if action in ("granted", "regrant", "denied"):
        gdpr.apply_action(contact, action, user_text, msg["id"])
        contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
        turn_note = ("El cliente acaba de ACEPTAR el seguimiento. Agradécelo en pocas palabras y continúa la conversación donde iba."
                     if action != "denied" else
                     "El cliente ha dicho NO al seguimiento. Respétalo: no programes seguimientos y sigue atendiendo lo que pida.")
    if first_contact:
        turn_note += ("\nEs el primer mensaje del cliente. Tras tu respuesta el sistema le pedirá permiso para el seguimiento, "
                      "así que en ESTE turno no termines con una pregunta de sí/no: termina con una pregunta abierta "
                      "(p. ej. qué día le viene mejor) o con información.")

    if contact["consent_state"] != "revoked":
        memory.add_memory(contact["id"], "message", f"[{now:%Y-%m-%d}] Cliente: {user_text}")

    if contact["status"] == "handoff":
        return {"status": "handoff_waiting_human"}

    reply = run_agent(contact, msg["id"], user_text, image_url, turn_note, trigger="inbound")
    contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
    if reply:
        outbound.send(contact, reply, origin="agent")
    if first_contact:
        text = gdpr.consent_request_text()
        outbound.send(contact, text, origin="gdpr")
        gdpr.mark_requested(contact["id"], text)
    return {"status": "replied", "reply": reply, "avoided": avoided}


# ---------------------------------------------------------------- agente

def run_agent(contact: dict, msg_id: int, user_text: str, image_url: str | None, turn_note: str, trigger: str) -> str:
    llm = get_llm()
    messages = [{"role": "system", "content": build_system(contact, user_text, turn_note)}]
    messages += history(contact["id"], exclude_id=msg_id)
    current: list | str
    if image_url:
        current = [{"type": "text", "text": user_text or "(el cliente envía esta imagen sin texto)"},
                   {"type": "image_url", "image_url": {"url": image_url}}]
    else:
        current = user_text or "(mensaje vacío)"
    if messages[-1]["role"] == "user" and isinstance(current, str):
        messages[-1]["content"] += "\n" + current
    else:
        messages.append({"role": "user", "content": current})

    trace, t0, tin, tout, error, reply = [], time.monotonic(), 0, 0, None, None
    schemas = tools.schemas()
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            res = llm.chat(messages, tools=schemas)
            tin += res.input_tokens or 0
            tout += res.output_tokens or 0
            if not res.tool_calls:
                reply = (res.content or "").strip()
                break
            messages.append(res.raw_assistant)
            for call in res.tool_calls:
                fresh = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact["id"],))
                result = tools.dispatch(fresh, call.name, call.arguments)
                trace.append({"tool": call.name, "args": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": tools.to_json(result)})
        else:
            reply = "Perdona, dame un momento que lo reviso con el equipo y te digo."
            tools.dispatch(contact, "handoff_to_human", {"reason": "bucle de herramientas", "summary": user_text[:300]})
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo del agente")
        error = str(e)[:500]
        reply = "Perdona, he tenido un problema técnico. Te escribe una persona del equipo en breve."
        tools.dispatch(contact, "handoff_to_human", {"reason": "error técnico del agente", "summary": user_text[:300]})
    db.execute(
        "INSERT INTO agent_runs(contact_id, trigger, model, latency_ms, tool_calls, input_tokens, output_tokens, error, created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (contact["id"], trigger, llm.model, int((time.monotonic() - t0) * 1000), json.dumps(trace, ensure_ascii=False, default=str),
         tin or None, tout or None, error, clock.now()),
    )
    return reply or ""


def compose_followup(contact: dict, f: dict) -> str:
    """Redacta un seguimiento personalizado con memoria. Sin tools: solo texto."""
    llm = get_llm()
    _, _, template = outbound.plan(contact, "followup", scheduler.sequences()[f["sequence"]]["category"])
    seq = scheduler.sequences()[f["sequence"]]
    goal = f["reason"]
    note = f.get("note") or ""
    constraints = (
        "Se enviará como plantilla de WhatsApp que ya empieza por 'Hola {nombre},'. Escribe SOLO la continuación, "
        "una única línea sin saltos, máximo 280 caracteres."
        if template else
        "Escribe SOLO el mensaje (máx. 300 caracteres), natural, como lo escribiría una persona."
    )
    turn_note = (
        f"Vas a escribir un mensaje de SEGUIMIENTO proactivo (secuencia '{f['sequence']}', paso {f['step']}: {seq['description']}).\n"
        f"Objetivo de este mensaje: {goal}\nNota: {note}\n{constraints}\n"
        "Usa los hechos recordados (nombre, día de cobro, objeción) si encajan, sin sonar a que lees una ficha. "
        "Si hay un pago pendiente, incluye su enlace. No uses herramientas."
    )
    messages = [{"role": "system", "content": build_system(contact, goal + " " + note, turn_note)}]
    messages += history(contact["id"], n=10)
    messages.append({"role": "user", "content": "[SISTEMA] Redacta ahora el mensaje de seguimiento."})
    t0 = time.monotonic()
    res = llm.chat(messages, tools=None)
    text = " ".join((res.content or "").split()) if template else (res.content or "").strip()
    db.execute(
        "INSERT INTO agent_runs(contact_id, trigger, model, latency_ms, tool_calls, input_tokens, output_tokens, created_at) "
        "VALUES (%s,'followup',%s,%s,'[]',%s,%s,%s)",
        (contact["id"], llm.model, int((time.monotonic() - t0) * 1000), res.input_tokens, res.output_tokens, clock.now()),
    )
    return text[:300] if template else text[:1000]
