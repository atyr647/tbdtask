"""The main window: a sidebar nav driving a swappable content area.

Screens are created lazily on first navigation and reused thereafter; a
fresh ``refresh()`` runs on every show, so data is never stale but
widgets aren't rebuilt from scratch unless their content changed.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

from . import context, theme
from .screens.absences import AbsencesScreen
from .screens.alerts import AlertsScreen
from .screens.personnel import PersonnelScreen
from .screens.quals import QualsScreen
from .screens.templates import TemplatesScreen
from .screens.today import TodayScreen
from .screens.worklists import WorklistsScreen

# (key, label, screen class). Order defines the sidebar.
NAV = [
    ("today", "Today", TodayScreen),
    ("personnel", "Personnel", PersonnelScreen),
    ("quals", "Qualifications", QualsScreen),
    ("absences", "Absences", AbsencesScreen),
    ("worklists", "Worklists", WorklistsScreen),
    ("templates", "Recurring", TemplatesScreen),
    ("alerts", "Alerts", AlertsScreen),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Worklist Tracker")
        self.geometry("1180x760")
        self.minsize(940, 600)
        self.fonts = theme.apply(self)

        outer = ttk.Frame(self, style="TFrame")
        outer.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(outer, bg=theme.SIDEBAR, width=190)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        self.content = ttk.Frame(outer, style="TFrame")
        self.content.pack(side="left", fill="both", expand=True)

        self._screens: dict[str, object] = {}
        self._classes = {k: cls for k, _, cls in NAV}
        self._current: str | None = None
        self._nav_buttons: dict[str, tk.Label] = {}

        self.show("today")
        self.bind("<Control-r>", lambda e: self.refresh_current())

    def _build_sidebar(self):
        tk.Label(
            self.sidebar,
            text="Worklist\nTracker",
            bg=theme.SIDEBAR,
            fg="white",
            font=self.fonts.h2,
            justify="left",
            anchor="w",
            padx=18,
            pady=18,
        ).pack(fill="x")
        self._nav_buttons = {}
        for key, label, _ in NAV:
            btn = tk.Label(
                self.sidebar,
                text=label,
                bg=theme.SIDEBAR,
                fg="#c7d0e6",
                font=self.fonts.bold,
                anchor="w",
                padx=18,
                pady=11,
                cursor="hand2",
            )
            btn.pack(fill="x")
            btn.bind("<Button-1>", lambda e, k=key: self.show(k))
            btn.bind("<Enter>", lambda e, b=btn, k=key: self._hover(b, k, True))
            btn.bind("<Leave>", lambda e, b=btn, k=key: self._hover(b, k, False))
            self._nav_buttons[key] = btn

        spacer = tk.Frame(self.sidebar, bg=theme.SIDEBAR)
        spacer.pack(fill="both", expand=True)
        tk.Label(
            self.sidebar,
            text="Offline · local data",
            bg=theme.SIDEBAR,
            fg="#6b7595",
            font=self.fonts.small,
            padx=18,
            pady=10,
        ).pack(fill="x")

    def _hover(self, btn, key, entering):
        if key == self._current:
            return
        btn.configure(bg="#283150" if entering else theme.SIDEBAR)

    def _highlight(self, key):
        for k, btn in self._nav_buttons.items():
            if k == key:
                btn.configure(bg=theme.SIDEBAR_ACTIVE, fg="white")
            else:
                btn.configure(bg=theme.SIDEBAR, fg="#c7d0e6")

    def show(self, key: str, **params):
        """Navigate to a screen, passing params straight to its refresh()."""
        screen = self._screens.get(key)
        if screen is None:
            screen = self._classes[key](self.content, self)
            self._screens[key] = screen
        if self._current and self._current != key:
            self._screens[self._current].pack_forget()
        elif self._current == key:
            screen.pack_forget()
        screen.pack(fill="both", expand=True)
        self._current = key
        self._highlight(key)
        screen.on_show(**params)

    def refresh_current(self):
        if self._current:
            self._screens[self._current].on_show()


def main() -> int:
    context.bootstrap()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
