"""Small reusable Tk building blocks shared across screens."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import theme


class VScroll(ttk.Frame):
    """A vertically scrollable container. Put content in ``.body``.

    Uses a Canvas + inner frame (the standard Tk idiom) and binds the
    mouse wheel only while the pointer is over the widget, so multiple
    scroll regions on one screen don't fight.
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=theme.BG, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_config)
        self.canvas.bind("<Configure>", self._on_canvas_config)
        self.canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda e: self._unbind_wheel())

    def _on_body_config(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_config(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _unbind_wheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _wheel(self, e):
        if e.num == 5 or e.delta < 0:
            self.canvas.yview_scroll(1, "units")
        elif e.num == 4 or e.delta > 0:
            self.canvas.yview_scroll(-1, "units")


class Card(ttk.Frame):
    """A panel with an optional title, padded interior in ``.body``."""

    def __init__(self, master, title: str | None = None, **kw):
        super().__init__(master, style="Card.TFrame", padding=0, **kw)
        self._outer = tk.Frame(self, bg=theme.BORDER, bd=0)
        self._outer.pack(fill="both", expand=True)
        inner = ttk.Frame(self._outer, style="Card.TFrame", padding=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if title:
            ttk.Label(inner, text=title, style="CardH2.TLabel").pack(
                anchor="w", pady=(0, 8)
            )
        self.body = ttk.Frame(inner, style="Card.TFrame")
        self.body.pack(fill="both", expand=True)


class StatTile(Card):
    """Big-number tile: value on top, caption under it."""

    def __init__(self, master, value, caption: str, color: str | None = None, **kw):
        super().__init__(master, **kw)
        lbl = ttk.Label(self.body, text=str(value), style="Stat.TLabel")
        if color:
            lbl.configure(foreground=color)
        lbl.pack(anchor="w")
        ttk.Label(self.body, text=caption, style="StatCaption.TLabel").pack(anchor="w")


def badge(
    master, text: str, status: str | None = None, severity: str | None = None
) -> tk.Label:
    """A small coloured pill. Pass either a qual/task ``status`` or a
    ``severity`` to pick the colour."""
    if severity is not None:
        fg, bg = theme.severity_colors(severity)
    else:
        fg, bg = theme.status_colors(status)
    return tk.Label(
        master,
        text=text,
        bg=bg,
        fg=fg,
        padx=7,
        pady=1,
        font=("TkDefaultFont", 9, "bold"),
    )


class SearchableTree(ttk.Frame):
    """A ttk.Treeview with a live filter box above it.

    ``columns`` is a list of (key, heading, width). ``rows`` is a list of
    dicts keyed by column key plus an optional ``_id`` and ``_tags``. The
    filter matches a case-insensitive substring across all visible cells.
    """

    def __init__(
        self,
        master,
        columns,
        on_open: Callable[[str], None] | None = None,
        search_label: str = "Search",
        **kw,
    ):
        super().__init__(master, **kw)
        self._columns = columns
        self._all_rows: list[dict] = []
        self._on_open = on_open

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text=f"{search_label}:", style="TLabel").pack(side="left")
        self._query = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self._query, width=32)
        ent.pack(side="left", padx=(6, 0))
        self._query.trace_add("write", lambda *_: self._apply_filter())
        self._count = ttk.Label(bar, text="", style="Muted.TLabel")
        self._count.pack(side="right")

        keys = [c[0] for c in columns]
        self.tree = ttk.Treeview(
            self, columns=keys, show="headings", selectmode="browse"
        )
        for key, heading, width in columns:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=(width >= 200))
        vbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        if on_open:
            self.tree.bind("<Double-1>", self._handle_open)
            self.tree.bind("<Return>", self._handle_open)

        # Zebra striping + status tags.
        self.tree.tag_configure("odd", background=theme.PANEL2)
        self.tree.tag_configure("even", background=theme.PANEL)
        for st, (fg, bg) in theme.STATUS_COLORS.items():
            self.tree.tag_configure(f"st_{st}", background=bg, foreground=fg)

    def set_rows(self, rows: list[dict]) -> None:
        self._all_rows = rows
        self._apply_filter()

    def _apply_filter(self) -> None:
        q = self._query.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        keys = [c[0] for c in self._columns]
        shown = 0
        for i, row in enumerate(self._all_rows):
            values = [row.get(k, "") for k in keys]
            if q and q not in " ".join(str(v) for v in values).lower():
                continue
            tags = list(row.get("_tags", ()))
            tags.append("odd" if shown % 2 else "even")
            self.tree.insert(
                "", "end", iid=str(row.get("_id", i)), values=values, tags=tags
            )
            shown += 1
        total = len(self._all_rows)
        self._count.configure(text=f"{shown} of {total}" if q else f"{total}")

    def _handle_open(self, _e):
        sel = self.tree.selection()
        if sel and self._on_open:
            self._on_open(sel[0])


def empty_state(master, message: str) -> ttk.Label:
    return ttk.Label(master, text=message, style="Muted.TLabel")
