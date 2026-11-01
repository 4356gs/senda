"""API HTTP: webhook de WhatsApp, simulador, dashboard, pagos simulados y derechos RGPD.

Arranque: uvicorn senda.web.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import agent, clock, db, gdpr, metrics, payments, scheduler
from ..channels import outbound, whatsapp
from ..config import get_business, get_settings

log = logging.getLogger("senda.web")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_schema()
    yield
    db.close_pool()


app = FastAPI(title="Senda", version="0.1.0", lifespan=lifespan)


def admin(request: Request) -> None:
    token = get_settings().dashboard_token
    if not token:
        return
    given = request.query_params.get("token") or request.headers.get("x-senda-token") or request.cookies.get("senda_token")
    if given != token:
        raise HTTPException(401, "Token requerido")


def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "business": get_business(), "settings": get_settings(), **kw}


@app.get("/health")
def health():
    db.fetchone("SELECT 1 AS ok")
    return {"ok": True, "whatsapp": get_settings().whatsapp_enabled, "llm": get_settings().llm_provider,
            "model": get_settings().agent_model, "clock": clock.now().isoformat()}


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


# ------------------------------------------------------------ WhatsApp Cloud API

@app.get("/webhook")
def verify(hub_mode: str = Query(None, alias="hub.mode"), hub_token: str = Query(None, alias="hub.verify_token"),
           hub_challenge: str = Query(None, alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_token == get_settings().wa_verify_token:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(403, "verify token inválido")


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "firma inválida")
    payload = await request.json()
    for item in whatsapp.parse_webhook(payload):
        background.add_task(_process_whatsapp, item)
    return {"ok": True}  # responder rápido: Meta reintenta si tardamos


def _process_whatsapp(item: dict) -> None:
    try:
        if item["media_id"]:
            data, mime = whatsapp.download_media(item["media_id"])
            if item["kind"] == "audio":
                item["audio_bytes"] = data
            else:
                item["image_bytes"], item["image_mime"] = data, mime
            item["media_ref"] = item["media_id"]
        item["channel"] = "whatsapp"
        agent.handle_inbound(item)
    except Exception:  # noqa: BLE001
        log.exception("Error procesando mensaje de WhatsApp %s", item.get("wa_message_id"))


# ------------------------------------------------------------ Simulador (demo sin número real)

@app.get("/simulador", response_class=HTMLResponse, dependencies=[Depends(admin)])
def simulator(request: Request):
    return templates.TemplateResponse(request, "simulator.html", _ctx(request))


@app.post("/api/sim/inbound", dependencies=[Depends(admin)])
async def sim_inbound(background: BackgroundTasks, wa_id: str = Form(...), name: str = Form(None),
                      text: str = Form(None), audio: UploadFile | None = File(None), image: UploadFile | None = File(None)):
    item = {"wa_id": wa_id, "name": name, "channel": "simulator", "kind": "text", "text": text or None}
    if audio is not None and audio.filename:
        item.update(kind="audio", audio_bytes=await audio.read(), media_ref=audio.filename)
    if image is not None and image.filename:
        item.update(kind="image", image_bytes=await image.read(), image_mime=image.content_type, media_ref=image.filename)
    background.add_task(agent.handle_inbound, item)
    return {"queued": True}


@app.get("/api/sim/{wa_id}/messages", dependencies=[Depends(admin)])
def sim_messages(wa_id: str):
    c = db.fetchone("SELECT * FROM contacts WHERE wa_id=%s", (wa_id,))
    if not c:
        return {"contact": None, "messages": [], "followups": []}
    msgs = db.fetchall(
        "SELECT id, direction, kind, body, transcript, origin, pricing_category, cost_eur, created_at "
        "FROM messages WHERE contact_id=%s ORDER BY created_at, id", (c["id"],))
    return {"contact": c, "messages": msgs, "followups": scheduler.pending_for(c["id"]),
            "typing": agent._locks[wa_id].locked()}


@app.post("/api/demo/advance", dependencies=[Depends(admin)])
def demo_advance(hours: float = Form(24)):
    clock.advance(hours)
    res = scheduler.run_due()
    return {"now": clock.local_now().isoformat(), "executed": res}


@app.post("/api/demo/reset", dependencies=[Depends(admin)])
def demo_reset():
    with db.tx() as c:
        c.execute("TRUNCATE contacts, consent_events, messages, contact_facts, memory_items, followups, appointments, "
                  "payments, handoffs, agent_runs RESTART IDENTITY CASCADE")
    clock.reset()
    return {"ok": True}


# ------------------------------------------------------------ Pagos (Bizum simulado)

@app.get("/pagar/{token}", response_class=HTMLResponse)
def pay_page(request: Request, token: str):
    p = db.fetchone("SELECT p.*, c.display_name FROM payments p JOIN contacts c ON c.id=p.contact_id WHERE token=%s", (token,))
    if not p:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "pay.html", _ctx(request, p=p))


@app.post("/pagar/{token}")
def pay_confirm(token: str):
    row = payments.mark_paid(token)
    if row:
        contact = db.fetchone("SELECT * FROM contacts WHERE id=%s", (row["contact_id"],))
        outbound.send(contact, f"¡Recibido! Hemos registrado tu señal de {row['amount_eur']} €. Tu cita queda confirmada. "
                               f"Te recordaremos el día antes.", origin="payment")
    return RedirectResponse(f"/pagar/{token}", status_code=303)


# ------------------------------------------------------------ Dashboard

@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(admin)])
def dashboard(request: Request):
    resp = templates.TemplateResponse(request, "dashboard.html", _ctx(request))
    if request.query_params.get("token"):
        resp.set_cookie("senda_token", request.query_params["token"], httponly=True, samesite="lax")
    return resp


@app.get("/api/dashboard", dependencies=[Depends(admin)])
def dashboard_data():
    return JSONResponse(jsonable({
        "now": clock.local_now().isoformat(),
        "kpis": metrics.kpis(),
        "consent_log": db.fetchall(
            "SELECT e.created_at, e.event, e.purpose, e.legal_basis, e.evidence_text, e.policy_version, c.wa_id, c.display_name "
            "FROM consent_events e JOIN contacts c ON c.id=e.contact_id ORDER BY e.created_at DESC, e.id DESC LIMIT 40"),
        "followups": db.fetchall(
            "SELECT f.id, f.sequence, f.step, f.due_at, f.status, f.outcome, f.reason, c.wa_id, c.display_name "
            "FROM followups f JOIN contacts c ON c.id=f.contact_id "
            "ORDER BY (f.status='scheduled') DESC, f.due_at DESC LIMIT 40"),
        "contacts": db.fetchall(
            "SELECT c.id, c.wa_id, c.display_name, c.channel, c.consent_state, c.stage, c.status, c.last_inbound_at, "
            "(SELECT count(*) FROM messages m WHERE m.contact_id=c.id) AS n_msgs, "
            "(SELECT json_object_agg(key, value) FROM contact_facts f WHERE f.contact_id=c.id) AS facts "
            "FROM contacts c WHERE c.status <> 'erased' ORDER BY COALESCE(c.last_inbound_at, c.created_at) DESC LIMIT 50"),
        "runs": db.fetchall(
            "SELECT r.created_at, r.trigger, r.model, r.latency_ms, r.tool_calls, r.error, c.wa_id "
            "FROM agent_runs r LEFT JOIN contacts c ON c.id=r.contact_id ORDER BY r.created_at DESC, r.id DESC LIMIT 25"),
        "handoffs": db.fetchall(
            "SELECT h.id, h.contact_id, h.reason, h.summary, h.created_at, c.wa_id, c.display_name "
            "FROM handoffs h JOIN contacts c ON c.id=h.contact_id WHERE h.resolved_at IS NULL ORDER BY h.created_at DESC"),
        "appointments": db.fetchall(
            "SELECT a.id, a.service, a.start_at, a.status, a.gcal_event_id IS NOT NULL AS gcal, c.wa_id, c.display_name, "
            "(SELECT status FROM payments p WHERE p.appointment_id=a.id ORDER BY id DESC LIMIT 1) AS pago "
            "FROM appointments a JOIN contacts c ON c.id=a.contact_id ORDER BY a.start_at DESC LIMIT 30"),
    }))


@app.post("/api/contacts/{contact_id}/human_reply", dependencies=[Depends(admin)])
def human_reply(contact_id: int, text: str = Form(...)):
    c = db.fetchone("SELECT * FROM contacts WHERE id=%s", (contact_id,))
    if not c:
        raise HTTPException(404)
    return outbound.send(c, text, origin="human")


@app.post("/api/contacts/{contact_id}/resume", dependencies=[Depends(admin)])
def resume_agent(contact_id: int):
    with db.tx() as c:
        c.execute("UPDATE contacts SET status='active' WHERE id=%s AND status='handoff'", (contact_id,))
        c.execute("UPDATE handoffs SET resolved_at=%s WHERE contact_id=%s AND resolved_at IS NULL", (clock.now(), contact_id))
    return {"ok": True}


@app.get("/api/contacts/{contact_id}/export", dependencies=[Depends(admin)])
def export(contact_id: int):
    return JSONResponse(jsonable(gdpr.export_contact(contact_id)))


@app.delete("/api/contacts/{contact_id}", dependencies=[Depends(admin)])
def erase(contact_id: int):
    gdpr.erase_contact(contact_id)
    return {"ok": True}


@app.get("/privacidad", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _ctx(request, purpose=gdpr.PURPOSE_FOLLOWUP))


def jsonable(obj):
    from decimal import Decimal

    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(obj, custom_encoder={Decimal: float})
