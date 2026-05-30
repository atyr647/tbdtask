"""Common screen scaffolding."""

from __future__ import annotations

import tkinter as tk
import traceback
from tkinter import ttk

from .. import theme


class Screen(ttk.Frame):
    """Base class for a navigable screen.

    Subclasses implement :meth:`build` (one-time widget construction is
    optional) and :meth:`refresh` (rebuild content from fresh data).
    The shell calls :meth:`on_show` whenever the screen becomes visible.
    """

    title: str = ""

    def __init__(self, master, app):
        super().__init__(master, style="TFrame", padding=18)
        self.app = app

    def on_show(self, **params) -> None:
        try:
            self.refresh(**params)
        except Exception:  # pragma: no cover - surfaced in UI
            self._show_error(traceback.format_exc())

    def refresh(self, **params) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    def clear(self) -> None:
        for child in self.winfo_children():
            child.destroy()

    def header(self, text: str, subtitle: str | None = None) -> ttk.Frame:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 14))
        ttk.Label(bar, text=text, style="H1.TLabel").pack(side="left")
        if subtitle:
            ttk.Label(bar, text=subtitle, style="Muted.TLabel").pack(
                side="left", padx=(12, 0), pady=(10, 0))
        return bar

    def _show_error(self, tb: str) -> None:
        self.clear()
        self.header("Something went wrong")
        box = tk.Text(self, height=20, wrap="word", bg=theme.PANEL,
                      fg=theme.BAD, relief="flat")
        box.insert("1.0", tb)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)
