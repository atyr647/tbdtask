"""Qualifications — readiness overview (gap highlighting) + colour matrix."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import Card, SearchableTree, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


class QualsScreen(Screen):
    title = "Qualifications"

    def refresh(self, tab: str = "overview", **_):
        self.clear()
        bar = self.header("Qualifications")
        ttk.Button(
            bar,
            text="+ New qualification",
            style="Accent.TButton",
            command=self._add_qual,
        ).pack(side="right")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        overview = ttk.Frame(nb, style="TFrame", padding=12)
        matrix = ttk.Frame(nb, style="TFrame", padding=12)
        catalog = ttk.Frame(nb, style="TFrame", padding=12)
        nb.add(overview, text="Readiness overview")
        nb.add(matrix, text="Matrix")
        nb.add(catalog, text="Catalog")
        self._build_overview(overview)
        self._build_matrix(matrix)
        self._build_catalog(catalog)
        nb.select({"matrix": 1, "catalog": 2}.get(tab, 0))

    # -- Catalog ----------------------------------------------------------
    def _build_catalog(self, parent):
        rows = read(Q.qual_catalog())
        if not rows:
            empty_state(parent, "No qualifications yet.").pack(anchor="w")
            return
        ttk.Label(
            parent,
            text="Double-click to rename; use the buttons to rename or archive.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        self._catalog_rows = {r["id"]: r for r in rows}
        table = SearchableTree(
            parent,
            columns=[
                ("name", "Name", 240),
                ("qualified", "Qualified", 90),
                ("in_progress", "In progress", 100),
                ("dinq", "DINQ", 70),
                ("validity", "Validity (d)", 100),
            ],
            on_open=lambda iid: self._edit_qual(int(iid)),
            search_label="Filter",
        )
        table.pack(fill="both", expand=True)
        table.set_rows(
            [
                {
                    "_id": r["id"],
                    "name": r["name"],
                    "qualified": r["qualified"],
                    "in_progress": r["in_progress"],
                    "dinq": r["dinq"],
                    "validity": r["validity_period_days"] or "",
                }
                for r in rows
            ]
        )
        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(
            btns, text="Rename selected", command=lambda: self._edit_selected(table)
        ).pack(side="left")
        ttk.Button(
            btns, text="Archive selected", command=lambda: self._archive_selected(table)
        ).pack(side="left", padx=4)

    def _selected_id(self, table):
        sel = table.tree.selection()
        return int(sel[0]) if sel else None

    def _edit_selected(self, table):
        qid = self._selected_id(table)
        if qid is not None:
            self._edit_qual(qid)

    def _archive_selected(self, table):
        qid = self._selected_id(table)
        if qid is not None:
            self._archive_qual(qid)

    def _add_qual(self):
        vals = forms.prompt(
            self,
            "New qualification",
            [forms.text("name", "Name", required=True)],
            submit_label="Create",
        )
        if not vals:
            return
        run_write(
            self,
            commands.create_qual(vals["name"]),
            on_done=lambda: self.app.show("quals", tab="catalog"),
        )

    def _edit_qual(self, qual_id):
        row = getattr(self, "_catalog_rows", {}).get(qual_id)
        initial = {"name": row["name"]} if row else {}
        vals = forms.prompt(
            self,
            "Rename qualification",
            [forms.text("name", "Name", required=True)],
            initial=initial,
            submit_label="Save",
        )
        if not vals:
            return
        run_write(
            self,
            commands.update_qual(qual_id, vals["name"]),
            on_done=lambda: self.app.show("quals", tab="catalog"),
        )

    def _archive_qual(self, qual_id):
        vals = forms.prompt(
            self,
            "Archive qualification",
            [forms.text("reason", "Reason")],
            submit_label="Archive",
        )
        if vals is None:
            return
        run_write(
            self,
            commands.archive_qual(qual_id, vals.get("reason") or ""),
            confirm=(
                "Archive qualification",
                "Hide this qualification from the catalog and matrix?",
            ),
            on_done=lambda: self.app.show("quals", tab="catalog"),
        )

    # -- Overview ---------------------------------------------------------
    def _build_overview(self, parent):
        summaries = read(Q.quals_overview(threshold=2))
        gaps = sum(1 for s in summaries if s.gap)
        ttk.Label(
            parent,
            text=f"{len(summaries)} qualifications · {gaps} below the "
            f"2-qualified threshold",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        scroller = VScroll(parent)
        scroller.pack(fill="both", expand=True)
        for su in summaries:
            card = Card(scroller.body)
            card.pack(fill="x", pady=4)
            top = ttk.Frame(card.body, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(
                top, text=su.name, style="Card.TLabel", font=theme.Fonts().bold
            ).pack(side="left")
            if su.gap:
                badge(top, "GAP", status="dinq").pack(side="left", padx=8)
            counts = "   ".join(f"{k}: {v}" for k, v in su.counts.items() if v)
            ttk.Label(
                top, text=counts or "no assignments", style="CardMuted.TLabel"
            ).pack(side="right")

            line = ttk.Frame(card.body, style="Card.TFrame")
            line.pack(fill="x", pady=(6, 0))
            self._name_row(line, "Qualified", su.qualified_names, "qualified")
            if su.in_progress_names:
                self._name_row(
                    card.body, "In progress", su.in_progress_names, "in_progress"
                )
            if su.dinq_names:
                self._name_row(card.body, "DINQ", su.dinq_names, "dinq")
            if su.expiring_soon:
                exp = ", ".join(f"{n} ({d})" for n, d in su.expiring_soon)
                ttk.Label(
                    card.body, text=f"Expiring soon: {exp}", style="CardMuted.TLabel"
                ).pack(anchor="w", pady=(4, 0))

    def _name_row(self, parent, label, names, status):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=1)
        badge(row, label, status=status).pack(side="left")
        ttk.Label(
            row,
            text="  " + (", ".join(names) if names else "—"),
            style="Card.TLabel",
            wraplength=820,
            justify="left",
        ).pack(side="left")

    # -- Matrix -----------------------------------------------------------
    def _build_matrix(self, parent):
        data = read(Q.qual_matrix())
        if not data.rows:
            empty_state(parent, "No personnel to show.").pack(anchor="w")
            return

        legend = ttk.Frame(parent)
        legend.pack(fill="x", pady=(0, 8))
        ttk.Label(legend, text="Legend:", style="Muted.TLabel").pack(side="left")
        for st in ("qualified", "in_progress", "assigned", "dinq", "expired"):
            badge(
                legend,
                theme.STATUS_GLYPH.get(st, st) + " " + st.replace("_", " "),
                status=st,
            ).pack(side="left", padx=3)

        # Both-axis scrollable canvas of coloured cells.
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=theme.BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(wrap, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        canvas.grid(row=0, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        grid = ttk.Frame(canvas, style="Card.TFrame")
        canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind("<Enter>", lambda e: self._wheel_bind(canvas))
        canvas.bind("<Leave>", lambda e: self._wheel_unbind(canvas))

        small = theme.Fonts().small
        # Header row: corner + qual names (truncated, full name as tooltip text)
        corner = tk.Label(
            grid,
            text="Name",
            bg=theme.PANEL3,
            fg=theme.TEXT_DIM,
            font=theme.Fonts().bold,
            padx=8,
            pady=4,
            anchor="w",
            width=22,
        )
        corner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        for c, qn in enumerate(data.qual_names, start=1):
            lbl = tk.Label(
                grid,
                text=(qn[:10] + "…") if len(qn) > 11 else qn,
                bg=theme.PANEL3,
                fg=theme.TEXT_DIM,
                font=small,
                padx=4,
                pady=4,
                width=6,
            )
            lbl.grid(row=0, column=c, sticky="nsew", padx=1, pady=1)
            self._tooltip(lbl, qn)

        for r, mrow in enumerate(data.rows, start=1):
            name = mrow.name + (f"  ({mrow.title})" if mrow.title else "")
            bgc = theme.PANEL2 if r % 2 else theme.PANEL
            tk.Label(
                grid,
                text=name,
                bg=bgc,
                fg=theme.TEXT,
                anchor="w",
                font=small,
                padx=8,
                width=22,
            ).grid(row=r, column=0, sticky="nsew", padx=1, pady=1)
            for c, status in enumerate(mrow.cells, start=1):
                glyph = theme.STATUS_GLYPH.get(status or "", "")
                fg, cell_bg = (
                    theme.status_colors(status) if status else (theme.MUTED, bgc)
                )
                tk.Label(
                    grid,
                    text=glyph,
                    bg=cell_bg,
                    fg=fg,
                    font=small,
                    width=6,
                    padx=2,
                    pady=3,
                ).grid(row=r, column=c, sticky="nsew", padx=1, pady=1)

    def _wheel_bind(self, canvas):
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"),
        )
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    def _wheel_unbind(self, canvas):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.unbind_all(seq)

    def _tooltip(self, widget, text):
        tip = {"win": None}

        def enter(_e):
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height()
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(
                win,
                text=text,
                bg="#111",
                fg="white",
                padx=6,
                pady=2,
                font=theme.Fonts().small,
            ).pack()
            tip["win"] = win

        def leave(_e):
            if tip["win"]:
                tip["win"].destroy()
                tip["win"] = None

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
