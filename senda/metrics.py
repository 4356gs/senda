"""Métricas para el dashboard: conversión, RGPD y coste de mensajería."""
from __future__ import annotations

from . import db
from .config import get_settings


def kpis() -> dict:
    s = get_settings()
    c = db.fetchone("""
        SELECT count(*) AS leads,
               count(*) FILTER (WHERE consent_state='granted') AS consent_granted,
               count(*) FILTER (WHERE consent_state IN ('granted','denied','revoked')) AS consent_answered,
               count(*) FILTER (WHERE consent_state='revoked') AS opted_out,
               count(*) FILTER (WHERE status='handoff') AS handoffs_open,
               count(*) FILTER (WHERE stage='cliente') AS clientes
        FROM contacts WHERE status <> 'erased'""")
    m = db.fetchone("""
        SELECT count(*) FILTER (WHERE direction='in') AS inbound,
               count(*) FILTER (WHERE direction='out') AS outbound,
               count(*) FILTER (WHERE direction='in' AND kind='audio') AS audios,
               count(*) FILTER (WHERE direction='in' AND kind='image') AS images,
               count(*) FILTER (WHERE direction='out' AND pricing_category='service') AS free_msgs,
               count(*) FILTER (WHERE direction='out' AND pricing_category IN ('marketing','utility')) AS paid_msgs,
               COALESCE(sum(cost_eur),0) AS spent_eur
        FROM messages""")
    f = db.fetchone("""
        SELECT count(*) FILTER (WHERE status='sent') AS sent,
               count(*) FILTER (WHERE status='scheduled') AS scheduled,
               count(*) FILTER (WHERE status IN ('cancelled','skipped')) AS avoided,
               count(*) FILTER (WHERE status IN ('cancelled','skipped') AND sequence <> 'recordatorio_cita') AS avoided_marketing,
               count(*) FILTER (WHERE status IN ('cancelled','skipped') AND sequence = 'recordatorio_cita') AS avoided_utility
        FROM followups""")
    a = db.fetchone("SELECT count(*) FILTER (WHERE status='confirmed') AS citas FROM appointments")
    p = db.fetchone("""SELECT count(*) FILTER (WHERE status='paid') AS pagados, count(*) FILTER (WHERE status='pending') AS pendientes,
                       COALESCE(sum(amount_eur) FILTER (WHERE status='paid'),0) AS cobrado FROM payments""")
    r = db.fetchone("SELECT count(*) AS runs, avg(latency_ms)::int AS avg_ms, count(*) FILTER (WHERE error IS NOT NULL) AS errors FROM agent_runs")
    avoided_eur = f["avoided_marketing"] * s.price_marketing_eur + f["avoided_utility"] * s.price_utility_eur
    naive_eur = c["leads"] * s.naive_followups_per_lead * s.price_marketing_eur
    spent = float(m["spent_eur"])
    return {
        **c, **m, **f, **a, **p, **r,
        "spent_eur": round(spent, 4),
        "avoided_eur": round(avoided_eur, 4),
        "naive_eur": round(naive_eur, 4),
        "saved_vs_naive_eur": round(max(naive_eur - spent, 0), 4),
        "consent_rate": round(100 * c["consent_granted"] / c["consent_answered"], 1) if c["consent_answered"] else None,
        "conversion_rate": round(100 * a["citas"] / c["leads"], 1) if c["leads"] else None,
        "cobrado": float(p["cobrado"]),
    }
