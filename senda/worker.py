"""Worker de seguimientos: `python -m senda.worker`."""
from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from . import clock, db, memory, scheduler
from .config import get_settings

log = logging.getLogger("senda.worker")
_stop = False


def _handle(sig, frame):  # noqa: ARG001
    global _stop
    _stop = True


def recover_stuck() -> None:
    # Si un worker murió a mitad de envío, devuelve sus tareas a la cola
    db.execute(
        "UPDATE followups SET status='scheduled' WHERE status='running' AND created_at < %s AND executed_at IS NULL "
        "AND due_at < %s",
        (clock.now(), clock.now() - timedelta(minutes=10)),
    )


def tick() -> list[dict]:
    res = scheduler.run_due()
    for r in res:
        log.info("followup %s -> %s %s", r["id"], r["status"], r.get("outcome", ""))
    return res


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    db.init_schema()
    interval = get_settings().worker_interval_seconds
    last_purge = 0.0
    log.info("Worker de Senda arrancado (cada %ss)", interval)
    while not _stop:
        try:
            recover_stuck()
            tick()
            if time.time() - last_purge > 3600:
                n = memory.purge_expired()
                if n:
                    log.info("Retención: %s recuerdos caducados borrados", n)
                last_purge = time.time()
        except Exception:  # noqa: BLE001
            log.exception("Error en tick del worker")
        for _ in range(interval):
            if _stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
