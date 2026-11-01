"""LLM de reglas para tests y ensayo offline (LLM_PROVIDER=fake). NO es el producto: el producto usa Muse Spark."""
from __future__ import annotations

import json
import re
import uuid

from .llm import LLMResult, ToolCall


def _call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=args)


def _raw(calls: list[ToolCall]) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls]}


class FakeLLM:
    model = "fake-rules"

    def __init__(self) -> None:
        self.last_slots: list[str] = []
        self.transcript = "Hola, ¿tenéis hueco mañana para una limpieza dental?"

    def transcribe(self, wav_bytes: bytes, keywords=None) -> str:
        return self.transcript

    def chat(self, messages: list[dict], tools=None) -> LLMResult:
        last = messages[-1]
        if not tools:
            system = messages[0]["content"]
            url = re.search(r"https?://\S+/pagar/\S+", system)
            name = re.search(r'"nombre": "([^"]+)"', system)
            txt = f"{name.group(1) + ', ' if name else ''}te escribo por lo que hablamos de tu cita."
            if url:
                txt += f" Te dejo de nuevo el enlace de la señal: {url.group(0)}"
            else:
                txt += " ¿Te reservo uno de los huecos de esta semana?"
            return LLMResult(content=txt)

        if last["role"] == "tool":
            # respuesta final tras herramientas
            results = []
            for m in reversed(messages):
                if m["role"] != "tool":
                    break
                results.append(json.loads(m["content"]))
            for r in results:
                if "slots" in r and r["slots"]:
                    self.last_slots = r["slots"]
                    s = r["slots"][:2]
                    return LLMResult(content=f"Tengo libre {s[0][11:16]} o {s[1][11:16] if len(s) > 1 else ''}. ¿Qué hora te viene mejor?")
                if r.get("url"):
                    return LLMResult(content=f"Listo. Para confirmar, la señal por Bizum aquí: {r['url']}")
                if r.get("appointment_id"):
                    if r.get("senal_eur"):
                        c = [_call("create_payment_link", concept=f"Señal {r['servicio']}", amount_eur=r["senal_eur"],
                                   appointment_id=r["appointment_id"]),
                             _call("start_followup", sequence="pago_pendiente", note="falta la señal")]
                        return LLMResult(content=None, tool_calls=c, raw_assistant=_raw(c))
                    return LLMResult(content=f"¡Reservado! {r['servicio']} el {r['inicio'][:10]} a las {r['inicio'][11:16]}.")
                if r.get("programado"):
                    return LLMResult(content="Perfecto, te escribo ese día. ¡Gracias!")
                if r.get("nota", "").startswith("Equipo avisado"):
                    return LLMResult(content="Te paso con una persona del equipo, te escribe en un momento.")
            return LLMResult(content="Perfecto, cualquier cosa me dices.")

        text = last["content"] if isinstance(last["content"], str) else last["content"][0]["text"]
        t = text.lower().split("\n")[-1]
        calls: list[ToolCall] = []
        if "dolor" in t or "persona" in t:
            calls = [_call("handoff_to_human", reason="urgencia o petición de humano", summary=text[:200])]
        elif re.search(r"cobro el (\d+)", t):
            d = re.search(r"cobro el (\d+)", t).group(1)
            calls = [_call("remember", key="dia_cobro", value=f"el {d}"),
                     _call("schedule_followup_at", datetime_iso=f"2026-11-{int(d):02d}T10:00:00+01:00", note=f"cobra el {d}")]
        elif re.search(r"\b(la primera|a las|vale esa|me va bien)\b", t) and self.last_slots:
            svc = "blanqueamiento" if "blanque" in json.dumps(messages, ensure_ascii=False).lower() else "limpieza"
            calls = [_call("book_appointment", service_id=svc, start_iso=self.last_slots[0], patient_name="Carlos")]
        elif "blanque" in t:
            args = {"service_id": "blanqueamiento", "days": 5}
            if "semana que viene" in t:
                from datetime import timedelta

                from . import clock
                args["date_from"] = (clock.local_now().date() + timedelta(days=7)).isoformat()
            calls = [_call("set_lead_stage", stage="caliente", reason="pide blanqueamiento"),
                     _call("get_available_slots", **args)]
        elif any(w in t for w in ("hueco", "cita", "limpieza")):
            calls = [_call("set_lead_stage", stage="caliente", reason="pide cita"),
                     _call("get_available_slots", service_id="limpieza", days=5)]
        elif "me llamo" in t:
            calls = [_call("remember", key="nombre", value=t.split("me llamo")[-1].strip().title())]
        elif "precio" in t or "cuánto" in t or "cuanto" in t:
            calls = [_call("set_lead_stage", stage="objecion", reason="pregunta precio"),
                     _call("start_followup", sequence="presupuesto_sin_cita", note="preguntó precio")]
        if calls:
            return LLMResult(content=None, tool_calls=calls, raw_assistant=_raw(calls))
        return LLMResult(content="¡Hola! Soy Lucía, de la clínica. ¿En qué te puedo ayudar?")
