"""Flujo completo con el LLM de reglas: lead -> consentimiento -> cita -> señal -> seguimiento -> pago / BAJA."""
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from senda import agent, clock, db, memory, payments, scheduler
from senda.channels import outbound

WA = "34600111222"


def say(text, **kw):
    return agent.handle_inbound({"wa_id": WA, "name": "Carlos", "channel": "simulator", "kind": "text", "text": text, **kw})


def contact():
    return db.fetchone("SELECT * FROM contacts WHERE wa_id=%s", (WA,))


def outs():
    return db.fetchall("SELECT * FROM messages WHERE direction='out' ORDER BY id")


def test_full_conversion_flow():
    r = say("Hola, ¿tenéis hueco la semana que viene para un blanqueamiento?")
    assert r["status"] == "replied" and "Tengo libre" in r["reply"]
    c = contact()
    assert c["consent_state"] == "requested" and c["stage"] == "caliente"
    o = outs()
    assert len(o) == 2 and o[1]["origin"] == "gdpr" and "SÍ" in o[1]["body"]
    assert all(m["pricing_category"] == "service" and float(m["cost_eur"]) == 0 for m in o)

    say("SÍ")
    c = contact()
    assert c["consent_state"] == "granted"
    ev = db.fetchall("SELECT event, evidence_text FROM consent_events ORDER BY id")
    assert [e["event"] for e in ev] == ["requested", "granted"] and ev[1]["evidence_text"] == "SÍ"

    r = say("Me va bien la primera")
    assert "pagar/" in r["reply"]
    appt = db.fetchone("SELECT * FROM appointments")
    assert appt and appt["service"] == "blanqueamiento"
    pend = scheduler.pending_for(c["id"])
    seqs = {f["sequence"] for f in pend}
    assert {"pago_pendiente", "recordatorio_cita"} <= seqs
    assert contact()["stage"] == "pago_pendiente"

    # +23,6 h: recordatorio de pago (programado a +20 h, movido a las 09:30) aún dentro de la ventana -> gratis
    clock.advance(23.6)
    res = scheduler.run_due()
    assert any(x["status"] == "sent" for x in res)
    last = outs()[-1]
    assert last["origin"] == "followup" and "pagar/" in last["body"] and last["pricing_category"] == "service"

    # El cliente paga -> el segundo recordatorio de pago se cancela (mensaje evitado)
    token = db.fetchone("SELECT token FROM payments")["token"]
    payments.mark_paid(token)
    clock.advance(30)
    scheduler.run_due()
    pago = db.fetchall("SELECT status, outcome FROM followups WHERE sequence='pago_pendiente' ORDER BY step")
    assert pago[0]["status"] == "sent" and pago[1]["status"] == "cancelled" and "pago" in pago[1]["outcome"]
    assert contact()["stage"] == "cliente"


def test_followup_outside_window_uses_paid_template():
    say("¿cuánto cuesta una limpieza?")  # consent requested
    say("si")  # granted
    say("¿y el precio exacto?")  # -> presupuesto_sin_cita
    clock.advance(30)  # > 24 h: ventana cerrada
    scheduler.run_due()
    sent = [m for m in outs() if m["origin"] == "followup"]
    assert sent, "debió enviarse un seguimiento"
    assert sent[-1]["kind"] == "template" and sent[-1]["pricing_category"] == "marketing" and float(sent[-1]["cost_eur"]) > 0
    assert "\n" not in sent[-1]["body"]


def test_reply_cancels_pending_followups():
    say("cuánto cuesta")
    say("vale")
    say("y el precio?")
    assert scheduler.pending_for(contact()["id"])
    r = say("Hola de nuevo")
    assert r["avoided"] >= 1
    row = db.fetchone("SELECT count(*) n FROM followups WHERE outcome LIKE 'el cliente respondió%%'")
    assert row["n"] >= 1


def test_no_consent_no_followups():
    say("¿cuánto cuesta?")
    say("No")
    assert contact()["consent_state"] == "denied"
    say("vale, ¿y el precio?")
    assert not scheduler.pending_for(contact()["id"])


def test_baja_cancels_everything_and_wipes_memory():
    say("Hola, quiero cita para limpieza")
    say("sí")
    say("me llamo Carlos")
    cid = contact()["id"]
    assert memory.facts(cid)["nombre"] == "Carlos"
    say("cuanto cuesta")
    r = say("BAJA")
    assert r["status"] == "opted_out"
    c = contact()
    assert c["consent_state"] == "revoked" and c["status"] == "opted_out"
    assert not scheduler.pending_for(cid)
    assert memory.facts(cid) == {}
    assert db.fetchone("SELECT count(*) n FROM memory_items WHERE contact_id=%s", (cid,))["n"] == 0
    assert outs()[-1]["origin"] == "gdpr"


def test_payday_followup_and_memory():
    say("hola, cita para limpieza")
    say("si")
    say("Ahora no puedo pagar, cobro el 15")
    cid = contact()["id"]
    assert memory.facts(cid)["dia_cobro"] == "el 15"
    f = scheduler.pending_for(cid)
    assert f and f[0]["sequence"] == "personalizado"
    assert f[0]["due_at"].astimezone(ZoneInfo("Europe/Madrid")).day == 16  # el 15/11/2026 es domingo -> lunes 16


def test_handoff_pauses_agent():
    say("hola")
    r = say("Tengo mucho dolor en una muela")
    assert contact()["status"] == "handoff"
    assert db.fetchone("SELECT count(*) n FROM handoffs")["n"] == 1
    r = say("¿Hola?")
    assert r["status"] == "handoff_waiting_human"


def test_quiet_hours_and_sunday():
    tz = ZoneInfo("Europe/Madrid")
    sat_night = datetime(2026, 11, 7, 22, 0, tzinfo=tz)
    adj = scheduler.adjust_to_allowed(sat_night).astimezone(tz)
    assert adj.weekday() == 0 and adj.hour == 9 and adj.minute == 30


def test_audio_is_transcribed():
    wav = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                          "-c:a", "libopus", "-f", "ogg", "pipe:1"], capture_output=True, check=True).stdout
    r = say(None, kind="audio", audio_bytes=wav)
    m = db.fetchone("SELECT * FROM messages WHERE direction='in'")
    assert m["kind"] == "audio" and "limpieza" in m["transcript"]
    assert "Tengo libre" in r["reply"]


def test_window_logic():
    say("hola")
    c = contact()
    assert outbound.plan(c, "followup") == ("service", 0.0, False)
    clock.advance(25)
    cat, cost, tpl = outbound.plan(c, "followup")
    assert cat == "marketing" and cost > 0 and tpl
    assert outbound.plan(c, "reminder")[0] == "utility"


def test_sensitive_fact_rejected():
    say("hola")
    res = memory.remember_fact(contact()["id"], "contexto_personal", "está embarazada")
    assert res.startswith("No guardado")


def test_payment_reminder_outside_window_is_utility():
    say("Hola, ¿tenéis hueco la semana que viene para un blanqueamiento?")
    say("sí")
    say("Me va bien la primera")
    clock.advance(48)  # miércoles 10:00: los dos recordatorios de pago han vencido, ventana cerrada
    res = scheduler.run_due()
    assert sorted(r["status"] for r in res) == ["rescheduled", "sent"]  # nunca dos mensajes seguidos
    pago = [m for m in outs() if m["origin"] == "followup"]
    assert len(pago) == 1 and pago[0]["pricing_category"] == "utility" and pago[0]["kind"] == "template"
