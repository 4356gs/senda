"""Memoria de largo plazo por contacto.

Dos capas:
1. Hechos estructurados (contact_facts): nombre, día de cobro, objeción, preferencia de formato…
   Siempre se inyectan en el prompt: son baratos y deterministas.
2. Memoria semántica (memory_items + pgvector): cada mensaje relevante se embebe y se recupera
   por similitud cuando el contexto reciente no basta (conversaciones de 30 días).

Meta Model API no ofrece endpoint de embeddings, así que se usa un modelo multilingüe local
(fastembed / ONNX, 384 dim) que corre en CPU dentro del contenedor.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from functools import lru_cache

import numpy as np

from . import clock, db
from .config import get_settings

log = logging.getLogger(__name__)
DIM = 384

ALLOWED_FACT_KEYS = {
    "nombre": "Nombre con el que quiere que le traten",
    "dia_cobro": "Cuándo cobra / cuándo le viene bien pagar",
    "objecion": "Principal freno (precio, miedo, tiempo, pareja, etc.)",
    "tratamiento_interes": "Tratamiento o servicio que le interesa",
    "disponibilidad": "Franjas en las que puede venir",
    "preferencia_formato": "Si prefiere audios o texto",
    "contexto_personal": "Detalle no sensible útil para personalizar (p. ej. viene con su hija)",
    "presupuesto": "Presupuesto que ha mencionado",
}

# Categorías especiales (art. 9 RGPD) que NO se guardan como hechos de marketing
_SENSITIVE = re.compile(
    r"\b(embaraz|diabet|vih|sida|c[áa]ncer|depresi|ansiedad|medicaci|alerg|enfermedad|religi|pol[íi]tic|sexual)",
    re.I,
)


class _HashEmbedder:
    """Embeddings por hashing de n-gramas: sin dependencias, determinista. Solo tests/offline."""

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        out = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            t = t.lower()
            grams = [t[i : i + 3] for i in range(max(1, len(t) - 2))]
            for g in grams:
                h = int(hashlib.md5(g.encode()).hexdigest(), 16)
                v[h % DIM] += 1.0 if (h >> 8) & 1 else -1.0
            n = float(np.linalg.norm(v)) or 1.0
            out.append(v / n)
        return out


class _FastEmbedder:
    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> list[np.ndarray]:
        return [np.asarray(v, dtype=np.float32) for v in self.model.embed(texts)]


@lru_cache
def embedder():
    s = get_settings()
    if s.embeddings_provider == "fastembed":
        try:
            return _FastEmbedder(s.embeddings_model)
        except Exception as e:  # noqa: BLE001
            log.warning("fastembed no disponible (%s); usando embeddings hash", e)
    return _HashEmbedder()


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text or ""))


def remember_fact(contact_id: int, key: str, value: str) -> str:
    key = key.strip().lower()
    if key not in ALLOWED_FACT_KEYS:
        return f"Clave no permitida. Usa una de: {', '.join(ALLOWED_FACT_KEYS)}"
    if is_sensitive(value):
        return "No guardado: parece un dato de salud u otra categoría especial (art. 9 RGPD)."
    value = value.strip()[:300]
    now = clock.now()
    with db.tx() as c:
        c.execute(
            "INSERT INTO contact_facts(contact_id, key, value, updated_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (contact_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            (contact_id, key, value, now),
        )
        if key == "nombre":
            c.execute("UPDATE contacts SET display_name = %s WHERE id = %s", (value, contact_id))
        if key == "preferencia_formato":
            fmt = "audio" if "audio" in value.lower() else "texto"
            c.execute("UPDATE contacts SET preferred_format = %s WHERE id = %s", (fmt, contact_id))
    add_memory(contact_id, "fact", f"{key}: {value}")
    return "ok"


def facts(contact_id: int) -> dict[str, str]:
    rows = db.fetchall("SELECT key, value FROM contact_facts WHERE contact_id = %s ORDER BY key", (contact_id,))
    return {r["key"]: r["value"] for r in rows}


def add_memory(contact_id: int, kind: str, content: str) -> None:
    content = (content or "").strip()
    if len(content) < 4:
        return
    vec = embedder().embed([content[:1000]])[0]
    db.execute(
        "INSERT INTO memory_items(contact_id, kind, content, embedding, created_at) VALUES (%s,%s,%s,%s,%s)",
        (contact_id, kind, content[:2000], vec, clock.now()),
    )


def recall(contact_id: int, query: str, k: int = 5, exclude_recent: int = 12) -> list[dict]:
    """Recupera recuerdos semánticamente cercanos que ya no están en la ventana reciente."""
    if not query.strip():
        return []
    vec = embedder().embed([query[:1000]])[0]
    rows = db.fetchall(
        """
        WITH recent AS (
            SELECT id FROM memory_items WHERE contact_id = %(cid)s AND kind = 'message'
            ORDER BY created_at DESC LIMIT %(ex)s
        )
        SELECT kind, content, created_at, 1 - (embedding <=> %(v)s) AS score
        FROM memory_items
        WHERE contact_id = %(cid)s AND id NOT IN (SELECT id FROM recent)
        ORDER BY embedding <=> %(v)s
        LIMIT %(k)s
        """,
        {"cid": contact_id, "v": vec, "k": k, "ex": exclude_recent},
    )
    return [r for r in rows if r["score"] is not None and not math.isnan(r["score"]) and r["score"] > 0.25]


def purge_expired() -> int:
    """Limitación del plazo de conservación (art. 5.1.e): borra memoria de contactos inactivos."""
    days = get_settings().retention_days
    with db.tx() as c:
        cur = c.execute(
            """
            DELETE FROM memory_items m USING contacts ct
            WHERE m.contact_id = ct.id
              AND COALESCE(ct.last_inbound_at, ct.created_at) < %s - make_interval(days => %s)
            """,
            (clock.now(), days),
        )
        n = cur.rowcount
        c.execute(
            """
            DELETE FROM contact_facts f USING contacts ct
            WHERE f.contact_id = ct.id
              AND COALESCE(ct.last_inbound_at, ct.created_at) < %s - make_interval(days => %s)
            """,
            (clock.now(), days),
        )
    return n
