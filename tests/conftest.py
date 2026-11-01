import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_URL", "postgresql://senda:senda@localhost:5433/senda_test")
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDINGS_PROVIDER"] = "hash"
os.environ["DASHBOARD_TOKEN"] = ""
os.environ["WA_ACCESS_TOKEN"] = ""

import pytest  # noqa: E402

if not os.environ["DATABASE_URL"].rstrip("/").endswith("_test"):
    raise SystemExit("Los tests vacían la base de datos: usa una DATABASE_URL que termine en _test")

from senda import clock, db, llm  # noqa: E402

MONDAY_10 = datetime(2026, 11, 2, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))


@pytest.fixture(autouse=True)
def clean_db():
    db.init_schema()
    with db.tx() as c:
        c.execute("TRUNCATE settings, contacts, consent_events, messages, contact_facts, memory_items, followups, "
                  "appointments, payments, handoffs, agent_runs RESTART IDENTITY CASCADE")
    clock.set_now(MONDAY_10)
    llm.set_llm(None)
    yield


@pytest.fixture(scope="session", autouse=True)
def _close_pool():
    yield
    db.close_pool()
