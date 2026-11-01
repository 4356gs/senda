"""Configuración central (variables de entorno, ver .env.example)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Base de datos
    database_url: str = "postgresql://senda:senda@localhost:5432/senda"

    # --- Meta Model API (OpenAI-compatible)
    model_api_key: str = ""
    model_api_base_url: str = "https://api.meta.ai/v1"
    agent_model: str = "muse-spark-1.3"
    agent_reasoning_effort: str = "low"
    transcribe_model: str = "muse-voice-transcribe-1.0"
    llm_provider: str = "meta"  # meta | fake (tests / demo offline)

    # --- WhatsApp Cloud API
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_verify_token: str = "senda-verify"
    wa_app_secret: str = ""
    graph_api_version: str = ""  # obligatorio si usas WhatsApp real (p. ej. el de tu app en Meta)
    wa_template_followup: str = "senda_seguimiento"
    wa_template_reminder: str = "senda_recordatorio_cita"
    wa_template_lang: str = "es"

    # --- Google Calendar (opcional; si falta, agenda local en Postgres)
    google_service_account_file: str = ""
    google_calendar_id: str = ""

    # --- Negocio / políticas
    business_config: str = str(ROOT / "config" / "business.json")
    public_base_url: str = "http://localhost:8000"
    timezone: str = "Europe/Madrid"
    quiet_hours_start: int = 21   # no se escribe desde las 21:00
    quiet_hours_end: int = 9      # hasta las 09:00
    no_followups_on_sunday: bool = True
    max_followups_per_contact: int = 3
    retention_days: int = 30
    consent_policy_version: str = "2026-11-v1"

    # --- Precios WhatsApp (EUR por mensaje entregado). VERIFICA en la rate card oficial de Meta.
    price_marketing_eur: float = 0.0509
    price_utility_eur: float = 0.0166
    # Línea base "bot ingenuo": mensajes por lead que manda una secuencia fija sin inteligencia
    naive_followups_per_lead: int = 5

    # --- Embeddings para memoria (Meta Model API no ofrece embeddings)
    embeddings_provider: str = "fastembed"  # fastembed | hash
    embeddings_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # --- Worker
    worker_interval_seconds: int = 15
    dashboard_token: str = ""  # si se define, protege /dashboard y /api/admin

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.wa_access_token and self.wa_phone_number_id and self.graph_api_version)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_business() -> dict:
    with open(get_settings().business_config, encoding="utf-8") as f:
        return json.load(f)
