"""Agenda: Google Calendar (si hay credenciales) con espejo local en Postgres.

Los huecos se calculan con el horario de la clínica (config/business.json) menos lo ocupado:
citas locales + free/busy de Google Calendar. Así la demo funciona sin Google y en producción
la fuente de verdad es el calendario real del negocio.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from . import clock, db
from .config import get_business, get_settings

log = logging.getLogger(__name__)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SLOT_STEP_MIN = 15


def service(service_id: str) -> dict | None:
    sid = (service_id or "").strip().lower()
    for s in get_business()["services"]:
        if s["id"] == sid or s["name"].lower() == sid:
            return s
    return None


@lru_cache
def _gcal():
    s = get_settings()
    if not (s.google_service_account_file and s.google_calendar_id):
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            s.google_service_account_file, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001
        log.warning("Google Calendar no disponible: %s", e)
        return None


def _busy(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    rows = db.fetchall(
        "SELECT start_at, end_at FROM appointments WHERE status='confirmed' AND start_at < %s AND end_at > %s",
        (end, start),
    )
    busy = [(r["start_at"], r["end_at"]) for r in rows]
    g = _gcal()
    if g:
        try:
            fb = g.freebusy().query(body={
                "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "items": [{"id": get_settings().google_calendar_id}],
            }).execute()
            for b in fb["calendars"][get_settings().google_calendar_id]["busy"]:
                busy.append((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])))
        except Exception as e:  # noqa: BLE001
            log.warning("freebusy falló: %s", e)
    return busy


def available_slots(service_id: str, date_from: str | None = None, days: int = 5, limit: int = 6) -> list[str]:
    svc = service(service_id)
    if not svc:
        return []
    tz = clock.tz()
    now_local = clock.local_now()
    start_day = date.fromisoformat(date_from) if date_from else now_local.date()
    if start_day < now_local.date():
        start_day = now_local.date()
    window_start = datetime.combine(start_day, time(0), tz)
    window_end = window_start + timedelta(days=days + 1)
    busy = _busy(window_start, window_end)
    dur = timedelta(minutes=svc["duration_min"])
    out: list[str] = []
    hours = get_business()["opening_hours"]
    for d in range(days + 1):
        day = start_day + timedelta(days=d)
        ranges = hours.get(_DAYS[day.weekday()], [])
        for i in range(0, len(ranges), 2):
            t = datetime.combine(day, time.fromisoformat(ranges[i]), tz)
            close = datetime.combine(day, time.fromisoformat(ranges[i + 1]), tz)
            while t + dur <= close:
                if t > now_local + timedelta(hours=2) and not any(t < b_end and t + dur > b_start for b_start, b_end in busy):
                    out.append(t.isoformat())
                    if len(out) >= limit:
                        return out
                    t += timedelta(minutes=60)  # espaciado para ofrecer variedad
                    continue
                t += timedelta(minutes=SLOT_STEP_MIN)
    return out


def _is_bookable(start: datetime, end: datetime) -> bool:
    tz = clock.tz()
    ls, le = start.astimezone(tz), end.astimezone(tz)
    if ls < clock.local_now() + timedelta(hours=1) or ls.date() != le.date():
        return False
    ranges = get_business()["opening_hours"].get(_DAYS[ls.weekday()], [])
    inside = any(
        datetime.combine(ls.date(), time.fromisoformat(ranges[i]), tz) <= ls
        and le <= datetime.combine(ls.date(), time.fromisoformat(ranges[i + 1]), tz)
        for i in range(0, len(ranges), 2)
    )
    return inside and not any(ls < b_end and le > b_start for b_start, b_end in _busy(ls, le))


def book(contact_id: int, service_id: str, start_iso: str, patient_name: str | None) -> dict:
    svc = service(service_id)
    if not svc:
        return {"ok": False, "error": f"Servicio desconocido: {service_id}"}
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError:
        return {"ok": False, "error": "Fecha/hora inválida; usa ISO 8601 con zona horaria"}
    if start.tzinfo is None:
        start = start.replace(tzinfo=clock.tz())
    end = start + timedelta(minutes=svc["duration_min"])
    if not _is_bookable(start, end):
        alt = available_slots(svc["id"], start.astimezone(clock.tz()).date().isoformat(), days=3)
        return {"ok": False, "error": "Ese hueco ya no está libre o está fuera de horario", "alternativas": alt}
    gcal_id = None
    g = _gcal()
    if g:
        try:
            ev = g.events().insert(calendarId=get_settings().google_calendar_id, body={
                "summary": f"{svc['name']} — {patient_name or 'Paciente WhatsApp'}",
                "description": "Reservado por Senda (WhatsApp)",
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
            }).execute()
            gcal_id = ev.get("id")
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo crear evento en Google Calendar: %s", e)
    with db.tx() as c:
        row = c.execute(
            "INSERT INTO appointments(contact_id, service, patient_name, start_at, end_at, gcal_event_id, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (contact_id, svc["id"], patient_name, start, end, gcal_id, clock.now()),
        ).fetchone()
    return {"ok": True, "appointment_id": row["id"], "servicio": svc["name"], "inicio": start.isoformat(),
            "fin": end.isoformat(), "google_calendar": bool(gcal_id), "senal_eur": svc.get("deposit_eur", 0)}


def cancel(contact_id: int, appointment_id: int) -> dict:
    row = db.fetchone("SELECT * FROM appointments WHERE id=%s AND contact_id=%s", (appointment_id, contact_id))
    if not row:
        return {"ok": False, "error": "Cita no encontrada para este contacto"}
    g = _gcal()
    if g and row["gcal_event_id"]:
        try:
            g.events().delete(calendarId=get_settings().google_calendar_id, eventId=row["gcal_event_id"]).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo borrar evento GCal: %s", e)
    with db.tx() as c:
        c.execute("UPDATE appointments SET status='cancelled' WHERE id=%s", (appointment_id,))
        c.execute(
            "UPDATE followups SET status='cancelled', outcome='cita cancelada', executed_at=%s "
            "WHERE contact_id=%s AND status='scheduled' AND sequence='recordatorio_cita'",
            (clock.now(), contact_id),
        )
    return {"ok": True}


def upcoming(contact_id: int) -> list[dict]:
    return db.fetchall(
        "SELECT id, service, start_at FROM appointments WHERE contact_id=%s AND status='confirmed' AND start_at > %s ORDER BY start_at",
        (contact_id, clock.now()),
    )
