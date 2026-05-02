from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATA_DIR = Path(os.environ.get("TBDTASK_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / os.environ.get("TBDTASK_DB_FILE", "tbdtask.db")
DB_URL = f"sqlite:///{DB_PATH}"


class Base(DeclarativeBase):
    pass


engine = create_engine(DB_URL, echo=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401  ensure models are registered
    Base.metadata.create_all(engine)
    _ensure_columns()


# Lightweight forward-only column additions for SQLite. Each entry is
# (table, column_name, column_def). Skipped silently when the column is
# already present, so re-running on a fresh DB is a no-op.
_COLUMN_ADDITIONS = [
    ("persons", "arrival_date", "DATE"),
    ("persons", "sponsor_person_id", "INTEGER REFERENCES persons(id)"),
    ("persons", "orders_received", "BOOLEAN NOT NULL DEFAULT 0"),
    ("persons", "itinerary_received", "BOOLEAN NOT NULL DEFAULT 0"),
    ("persons", "aob_scheduled", "BOOLEAN NOT NULL DEFAULT 0"),
    ("persons", "barracks_assigned", "BOOLEAN NOT NULL DEFAULT 0"),
]


def _ensure_columns() -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, col, defn in _COLUMN_ADDITIONS:
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
