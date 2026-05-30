"""Centralised colours, fonts, and ttk styling.

The palette is lifted straight from ``app/static/app.css`` so the native
UI reads as the same product as the old web view. Status colours match
the qual-matrix / worklist legend exactly.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# --- palette (from app.css :root) -----------------------------------------
BG = "#dee5f3"
BG2 = "#cdd6e9"
PANEL = "#fbfcfe"
PANEL2 = "#f0f3fa"
PANEL3 = "#e6ebf5"
BORDER = "#c8d0e3"
TEXT = "#181d2c"
TEXT_DIM = "#3f4760"
MUTED = "#6a7388"
ACCENT = "#2856e7"
ACCENT2 = "#0a8a82"

GOOD = "#128353"
GOOD_SOFT = "#cdeadc"
WARN = "#b85f04"
WARN_SOFT = "#fae5c2"
BAD = "#c52b42"
BAD_SOFT = "#f7d2d8"
SIDEBAR = "#1c2438"
SIDEBAR_ACTIVE = "#2856e7"

# --- qualification / task status → (fg, bg) --------------------------------
# Mirrors the matrix legend: qualified green, in-progress amber, dinq red.
STATUS_COLORS: dict[str, tuple[str, str]] = {
    "qualified": (GOOD, GOOD_SOFT),
    "done": (GOOD, GOOD_SOFT),
    "in_progress": (WARN, WARN_SOFT),
    "assigned": (TEXT_DIM, PANEL3),
    "dinq": (BAD, BAD_SOFT),
    "expired": (BAD, BAD_SOFT),
    "waived": (MUTED, PANEL2),
    "not_assigned": (MUTED, PANEL),
    "open": (ACCENT, "#dbe4ff"),
    "carried": (MUTED, PANEL2),
    "discarded": (MUTED, PANEL2),
    "recurring": (ACCENT2, "#cdeae8"),
}

# Short glyphs for the dense matrix grid.
STATUS_GLYPH: dict[str, str] = {
    "qualified": "Q",
    "in_progress": "IP",
    "assigned": "A",
    "dinq": "DINQ",
    "expired": "EXP",
    "waived": "W",
    "not_assigned": "·",
}

SEVERITY_COLORS: dict[str, tuple[str, str]] = {
    "urgent": (BAD, BAD_SOFT),
    "critical": (BAD, BAD_SOFT),
    "danger": (BAD, BAD_SOFT),
    "warn": (WARN, WARN_SOFT),
    "warning": (WARN, WARN_SOFT),
    "info": (ACCENT, "#dbe4ff"),
}


def status_colors(status: str | None) -> tuple[str, str]:
    return STATUS_COLORS.get(status or "", (TEXT_DIM, PANEL2))


def severity_colors(sev: str | None) -> tuple[str, str]:
    return SEVERITY_COLORS.get(sev or "", (ACCENT, "#dbe4ff"))


class Fonts:
    """Lazily-created named fonts (Tk must have a root first)."""

    def __init__(self) -> None:
        base = "TkDefaultFont"
        self.body = tkfont.nametofont(base)
        self.body.configure(size=11)
        self.h1 = tkfont.Font(family=self.body.cget("family"), size=20, weight="bold")
        self.h2 = tkfont.Font(family=self.body.cget("family"), size=14, weight="bold")
        self.bold = tkfont.Font(family=self.body.cget("family"), size=11, weight="bold")
        self.small = tkfont.Font(family=self.body.cget("family"), size=9)
        self.mono = tkfont.Font(family="TkFixedFont", size=10)
        self.stat = tkfont.Font(family=self.body.cget("family"), size=24, weight="bold")


def apply(root: tk.Tk) -> Fonts:
    """Configure ttk styles + named fonts; return the font bundle."""
    fonts = Fonts()
    style = ttk.Style(root)
    # 'clam' is the most styleable cross-platform ttk theme and renders
    # cheaply on the Pi's software path.
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=TEXT, font=fonts.body)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL, relief="flat")
    style.configure("Sidebar.TFrame", background=SIDEBAR)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=fonts.small)
    style.configure(
        "CardMuted.TLabel", background=PANEL, foreground=MUTED, font=fonts.small
    )
    style.configure("H1.TLabel", background=BG, foreground=TEXT, font=fonts.h1)
    style.configure("H2.TLabel", background=BG, foreground=TEXT, font=fonts.h2)
    style.configure("CardH2.TLabel", background=PANEL, foreground=TEXT, font=fonts.h2)
    style.configure("Stat.TLabel", background=PANEL, foreground=ACCENT, font=fonts.stat)
    style.configure(
        "StatCaption.TLabel", background=PANEL, foreground=MUTED, font=fonts.small
    )

    style.configure(
        "TButton",
        background=PANEL2,
        foreground=TEXT,
        padding=(10, 5),
        relief="flat",
        borderwidth=0,
    )
    style.map("TButton", background=[("active", PANEL3), ("pressed", BORDER)])
    style.configure(
        "Accent.TButton", background=ACCENT, foreground="white", padding=(12, 6)
    )
    style.map("Accent.TButton", background=[("active", "#1c46c9")])

    # Treeview (used for dense tables and the qual matrix)
    style.configure(
        "Treeview",
        background=PANEL,
        fieldbackground=PANEL,
        foreground=TEXT,
        rowheight=26,
        borderwidth=0,
        font=fonts.body,
    )
    style.configure(
        "Treeview.Heading",
        background=PANEL3,
        foreground=TEXT_DIM,
        font=fonts.bold,
        relief="flat",
        padding=(6, 4),
    )
    style.map(
        "Treeview",
        background=[("selected", "#cdd9ff")],
        foreground=[("selected", TEXT)],
    )

    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=PANEL2,
        foreground=TEXT_DIM,
        padding=(14, 7),
        font=fonts.bold,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", PANEL)],
        foreground=[("selected", ACCENT)],
    )

    style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, padding=4)
    style.configure(
        "Vertical.TScrollbar",
        background=PANEL2,
        troughcolor=BG,
        borderwidth=0,
        arrowsize=12,
    )
    return fonts
