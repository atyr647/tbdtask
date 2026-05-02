from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATA_DIR = Path(os.environ.get("TBDTASK_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / os.environ.get("TBDTASK_DB_FILE", "tbdtask.db")
_DEFAULT_SQLITE_URL = f"sqlite:///{DB_PATH}"

# DATABASE_URL takes precedence so the same codebase runs as a hosted
# Postgres-backed service (Phase 3+) or as the offline SQLite AppImage
# without code changes. Falls back to the local SQLite file for the
# existing single-tenant flow.
DB_URL = os.environ.get("DATABASE_URL", _DEFAULT_SQLITE_URL)
IS_SQLITE = DB_URL.startswith("sqlite")


class Base(DeclarativeBase):
    pass


def _build_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, echo=False, future=True)
    # Postgres / others. ``pool_pre_ping`` survives idle-connection drops
    # behind reverse proxies and load balancers.
    return create_engine(url, echo=False, future=True, pool_pre_ping=True)


engine = _build_engine(DB_URL)


if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


# Postgres RLS hook: set app.current_org_id GUC on every connection checkout.
# This ensures RLS policies always have the tenant context, and fail-closed
# when no org is set (current_setting returns NULL, policy denies access).
if not IS_SQLITE:
    from .tenancy import current_org_id  # noqa: E402

    @event.listens_for(engine, "checkout")
    def _set_rls_org_id(dbapi_connection, connection_record, is_first):
        oid = current_org_id()
        if oid is not None:
            dbapi_connection.cursor().execute(
                "SET app.current_org_id = %s", (str(oid),)
            )
        else:
            # Fail-closed: unset the GUC so RLS policies deny access.
            dbapi_connection.cursor().execute(
                "RESET app.current_org_id"
            )


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Install the tenant-scoping listener at import time so every Session
# created anywhere in the app participates without per-call setup. The
# listener is a no-op until a ``tenant_context`` is active, so existing
# single-tenant code paths are unaffected in Phase 0.
from .tenancy import register_tenancy_listeners  # noqa: E402

register_tenancy_listeners()


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
    the schema in place) get a one-shot ``stamp`` at the initial revision
    so any newer migrations still apply on top of them.

    Set ``TBDTASK_SKIP_AUTOMIGRATE=1`` to short-circuit. The pytest suite
    sets this so tests don't hit the project's on-disk dev DB during the
    ``app.main`` import that ``create_app()`` triggers at module load.
    """
    if os.environ.get("TBDTASK_SKIP_AUTOMIGRATE"):
        return

    from . import models  # noqa: F401  ensure mappers are registered
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)

    # If this DB was created before Alembic was wired in, it already has
    # the initial-schema tables but no alembic_version row. Stamp it at
    # the initial revision (NOT head) so any newer migrations — like the
    # Phase 0 tenancy substrate — still apply on top of it.
    insp = inspect(engine)
    table_names = set(insp.get_table_names())
    has_app_tables = "persons" in table_names
    has_alembic_table = "alembic_version" in table_names
    if has_app_tables and not has_alembic_table:
        command.stamp(cfg, "ba3b919d6300")

    command.upgrade(cfg, "head")
