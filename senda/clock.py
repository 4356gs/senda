"""Reloj del sistema con desplazamiento para demos ("avanzar 24 h").

El desplazamiento vive en la tabla settings, así API y worker comparten la misma hora.
En producción el offset es 0 y no se toca.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config import get_settings

_OFFSET_KEY = "clock_offset_seconds"


def offset_seconds() -> int:
    row = db.fetchone("SELECT value FROM settings WHERE key = %s", (_OFFSET_KEY,))
    return int(row["value"]) if row else 0


def now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds())


def local_now() -> datetime:
    return now().astimezone(tz())


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def advance(hours: float) -> int:
    new = offset_seconds() + int(hours * 3600)
    db.execute(
        "INSERT INTO settings(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (_OFFSET_KEY, str(new)),
    )
    return new


def set_now(target: datetime) -> None:
    """Fija la hora del sistema (tests / ensayo de demo)."""
    delta = int((target - datetime.now(timezone.utc)).total_seconds())
    db.execute(
        "INSERT INTO settings(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (_OFFSET_KEY, str(delta)),
    )


def reset() -> None:
    db.execute("DELETE FROM settings WHERE key = %s", (_OFFSET_KEY,))
