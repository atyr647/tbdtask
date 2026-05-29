"""Native tkinter desktop front-end for the Worklist Tracker.

This package is a thin presentation layer over the existing
``app.services`` / ``app.models`` / ``app.db`` stack. It runs entirely
in-process: there is no HTTP server, no Jinja, and no WebKit/Tauri
webview. On a Raspberry Pi 400 that removes the browser engine — the
dominant cost of the old AppImage — and replaces it with native Tk
widgets that start instantly and repaint cheaply.

Layout
------
* ``context``  — runtime bootstrap: single-tenant flag, ``init_db()``,
  org resolution, and the ``read()`` / ``write()`` helpers that run a
  callback inside ``tenant_context`` + ``session_scope``.
* ``dto``      — plain dataclasses handed to the UI. ORM instances are
  never passed out of a session (lazy loads would explode), so every
  query funnels through these detached value objects.
* ``queries``  — functions that reuse ``app.services`` and return DTOs.
* ``theme``    — ttk styling + the qual/absence status colour map that
  mirrors the legacy spreadsheet legend.
* ``widgets``  — small reusable building blocks (cards, stat tiles,
  scrollable frames, searchable tables).
* ``screens``  — one module per top-level section.
* ``app``      — the main window: a sidebar nav driving a content area.

Run it with ``python -m app.tk`` (see ``__main__``).
"""

from __future__ import annotations

__all__ = ["main"]


def main() -> int:
    # Imported lazily so ``import app.tk`` stays cheap and doesn't pull in
    # Tk before the caller has decided to launch a window.
    from .app import main as _main

    return _main()
