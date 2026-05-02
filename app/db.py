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
    """Bring the configured DB up to the latest schema via Alembic.

    On a fresh install this creates every table at HEAD; on an existing
    one it applies any migrations that landed since the last run.
    Pre-Alembic databases (created back when ``_ensure_columns`` patched
    the schema in place) get a one-shot ``stamp head`` since their
    schema already matches the initial revision.
    """
    from . import models  # noqa: F401  ensure mappers are registered
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)

    # If this DB was created before Alembic was wired in, it already has
    # all the tables but no alembic_version row. Stamp it instead of
    # trying to re-run the initial migration (which would fail on table
    # already-exists).
    insp = inspect(engine)
    table_names = set(insp.get_table_names())
    has_app_tables = "persons" in table_names
    has_alembic_table = "alembic_version" in table_names
    if has_app_tables and not has_alembic_table:
        command.stamp(cfg, "head")
        return

    command.upgrade(cfg, "head")
