"""Cliente de modelos: Meta Model API (OpenAI-compatible) + transcripción Muse Voice Transcribe.

Docs: https://dev.meta.ai/docs/protocols/chat-completions · https://dev.meta.ai/docs/speech-to-text
Notas verificadas en la documentación:
- tool_choice solo admite "auto"; hay que reenviar el mensaje assistant con tool_calls antes de los role=tool.
- No se admiten `stop`, ni temperature+top_p a la vez.
- Audio: muse-spark-1.3 no soporta bien audio -> se transcribe antes con muse-voice-transcribe-1.0
  (WAV mono PCM16 16 kHz, máx. 10 min / 32 MB).
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_assistant: dict | None = None  # mensaje assistant tal cual para reenviarlo
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLM(Protocol):
    model: str

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult: ...

    def transcribe(self, wav_bytes: bytes, keywords: list[str] | None = None) -> str: ...


class MetaLLM:
    def __init__(self) -> None:
        from openai import OpenAI

        s = get_settings()
        if not s.model_api_key:
            raise RuntimeError("Falta MODEL_API_KEY (Meta Model API) en .env")
        self.settings = s
        self.model = s.agent_model
        self.client = OpenAI(base_url=s.model_api_base_url, api_key=s.model_api_key, max_retries=4, timeout=90)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "max_completion_tokens": 1200}
        if self.settings.agent_reasoning_effort:
            kwargs["reasoning_effort"] = self.settings.agent_reasoning_effort
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        raw_calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
            raw_calls.append(
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            )
        raw = {"role": "assistant", "content": msg.content}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        usage = getattr(resp, "usage", None)
        return LLMResult(
            content=msg.content,
            tool_calls=calls,
            raw_assistant=raw,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    def transcribe(self, wav_bytes: bytes, keywords: list[str] | None = None) -> str:
        s = self.settings
        req: dict[str, Any] = {"mode": "PUSH_TO_TALK", "model": s.transcribe_model, "audioEncoding": "WAV"}
        if keywords:
            req["keywords"] = keywords[:50]
        files = {
            "request": (None, json.dumps(req), "application/json"),
            "audio": ("audio.wav", wav_bytes, "audio/wav"),
        }
        url = s.model_api_base_url.rstrip("/") + "/asr/transcribe"
        delay = 0.5
        for attempt in range(4):
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {s.model_api_key}", "Accept": "text/plain"},
                files=files,
                timeout=120,
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            r.raise_for_status()
            return _extract_transcript(r)
        raise RuntimeError("Transcripción fallida tras reintentos")


def _extract_transcript(r: httpx.Response) -> str:
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype:
        return r.text.strip()
    data = r.json()
    for key in ("text", "transcript", "transcription"):
        if isinstance(data.get(key), str):
            return data[key].strip()
    segs = data.get("segments") or data.get("results") or []
    return " ".join(s.get("text", "") for s in segs if isinstance(s, dict)).strip()


def to_wav_16k_mono(data: bytes) -> bytes:
    """Convierte cualquier audio (OGG/Opus de WhatsApp, webm del navegador...) a WAV PCM16 16 kHz mono."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000",
         "-sample_fmt", "s16", "-t", "600", "-f", "wav", "pipe:1"],
        input=data,
        capture_output=True,
        check=True,
    )
    return proc.stdout


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        if get_settings().llm_provider == "fake":
            from .fake_llm import FakeLLM

            _llm = FakeLLM()
        else:
            _llm = MetaLLM()
    return _llm


def set_llm(llm: LLM | None) -> None:
    global _llm
    _llm = llm
