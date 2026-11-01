"""WhatsApp Cloud API: envío, descarga de media, parseo y verificación de webhooks."""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"


def _base() -> str:
    s = get_settings()
    if not s.graph_api_version:
        raise RuntimeError("Define GRAPH_API_VERSION en .env (la versión de Graph API de tu app de Meta)")
    return f"{GRAPH}/{s.graph_api_version}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().wa_access_token}"}


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    secret = get_settings().wa_app_secret
    if not secret:
        return True  # sin secreto configurado (desarrollo); en producción defínelo
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def send_text(to: str, text: str) -> str | None:
    s = get_settings()
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": True, "body": text[:4096]}}
    r = httpx.post(f"{_base()}/{s.wa_phone_number_id}/messages", json=payload, headers=_headers(), timeout=30)
    r.raise_for_status()
    return (r.json().get("messages") or [{}])[0].get("id")


def send_template(to: str, template: str, params: list[str]) -> str | None:
    s = get_settings()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": s.wa_template_lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": _clean_param(p)} for p in params]}],
        },
    }
    r = httpx.post(f"{_base()}/{s.wa_phone_number_id}/messages", json=payload, headers=_headers(), timeout=30)
    r.raise_for_status()
    return (r.json().get("messages") or [{}])[0].get("id")


def _clean_param(p: str) -> str:
    # Las variables de plantilla no admiten saltos de línea, tabs ni >4 espacios seguidos
    return " ".join((p or "").split())[:900]


def download_media(media_id: str) -> tuple[bytes, str]:
    r = httpx.get(f"{_base()}/{media_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    meta = r.json()
    r2 = httpx.get(meta["url"], headers=_headers(), timeout=60, follow_redirects=True)
    r2.raise_for_status()
    return r2.content, meta.get("mime_type", "application/octet-stream")


def parse_webhook(payload: dict) -> list[dict]:
    """Normaliza mensajes entrantes a dicts {wa_id, name, wa_message_id, kind, text, media_id, mime}."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                t = m.get("type")
                item = {
                    "wa_id": m.get("from"),
                    "name": names.get(m.get("from")),
                    "wa_message_id": m.get("id"),
                    "kind": "text",
                    "text": None,
                    "media_id": None,
                    "mime": None,
                }
                if t == "text":
                    item["text"] = m["text"]["body"]
                elif t in ("audio", "voice"):
                    item.update(kind="audio", media_id=m[t]["id"], mime=m[t].get("mime_type"))
                elif t == "image":
                    item.update(kind="image", media_id=m["image"]["id"], mime=m["image"].get("mime_type"),
                                text=m["image"].get("caption"))
                elif t == "button":
                    item["text"] = m["button"].get("text")
                elif t == "interactive":
                    inter = m["interactive"]
                    item["text"] = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title")
                else:
                    item["text"] = f"[mensaje de tipo {t} no soportado]"
                out.append(item)
    return out
