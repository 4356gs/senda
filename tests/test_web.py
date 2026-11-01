import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from senda import db
from senda.channels import whatsapp
from senda.config import get_settings
from senda.web.app import app

WEBHOOK = {
    "object": "whatsapp_business_account",
    "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "contacts": [{"profile": {"name": "Ana"}, "wa_id": "34611000000"}],
        "messages": [{"from": "34611000000", "id": "wamid.TEST1", "timestamp": "1", "type": "text",
                      "text": {"body": "Hola, ¿tenéis hueco para limpieza?"}}],
    }}]}],
}


def test_parse_webhook():
    items = whatsapp.parse_webhook(WEBHOOK)
    assert items[0]["wa_id"] == "34611000000" and items[0]["name"] == "Ana" and items[0]["kind"] == "text"


def test_signature(monkeypatch):
    monkeypatch.setattr(get_settings(), "wa_app_secret", "s3cret")
    body = json.dumps(WEBHOOK).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature(body, sig)
    assert not whatsapp.verify_signature(body, "sha256=bad")


def test_endpoints_end_to_end():
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"]
        assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "senda-verify",
                                              "hub.challenge": "42"}).text == "42"
        assert client.post("/webhook", json=WEBHOOK).status_code == 200
        # dedupe: mismo wamid otra vez
        client.post("/webhook", json=WEBHOOK)
        for _ in range(50):
            if db.fetchone("SELECT count(*) n FROM messages WHERE direction='out'")["n"] >= 2:
                break
            time.sleep(0.1)
        assert db.fetchone("SELECT count(*) n FROM messages WHERE direction='in'")["n"] == 1
        r = client.post("/api/sim/inbound", data={"wa_id": "34622000000", "name": "Luis", "text": "quiero cita para limpieza"})
        assert r.json()["queued"]
        d = client.get("/api/dashboard").json()
        assert d["kpis"]["leads"] >= 1 and isinstance(d["consent_log"], list)
        for path in ("/dashboard", "/simulador", "/privacidad"):
            assert client.get(path).status_code == 200
        r = client.post("/api/demo/advance", data={"hours": 24})
        assert "now" in r.json()
        cid = db.fetchone("SELECT id FROM contacts WHERE wa_id='34611000000'")["id"]
        exp = client.get(f"/api/contacts/{cid}/export").json()
        assert exp["mensajes"]
        assert client.delete(f"/api/contacts/{cid}").json()["ok"]
        assert db.fetchone("SELECT status FROM contacts WHERE id=%s", (cid,))["status"] == "erased"
