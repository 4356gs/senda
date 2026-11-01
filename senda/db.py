"""Acceso a PostgreSQL (psycopg 3, pool síncrono)."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ROOT, get_settings

_pool: ConnectionPool | None = None


def _configure(conn) -> None:
    from pgvector.psycopg import register_vector

    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=_configure,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[Any]:
    """Conexión transaccional: commit al salir, rollback en excepción."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def fetchone(sql: str, params: tuple | dict = ()) -> dict | None:
    with tx() as c:
        return c.execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple | dict = ()) -> list[dict]:
    with tx() as c:
        return c.execute(sql, params).fetchall()


def execute(sql: str, params: tuple | dict = ()) -> None:
    with tx() as c:
        c.execute(sql, params)


def init_schema() -> None:
    """Crea la extensión pgvector y las tablas (idempotente)."""
    import psycopg

    sql = Path(ROOT / "db" / "init.sql").read_text(encoding="utf-8")
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(sql)
