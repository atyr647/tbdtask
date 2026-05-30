"""Absences — list of windows + a colour-coded calendar grid."""

from __future__ import annotations

import tkinter as tk
from datetime import date
from tkinter import ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import SearchableTree, empty_state
from .. import queries as Q
from .base import Screen


def _absence_fields(person_choice=None, code_choices=None):
    fields = []
    if person_choice is not None:
        fields.append(forms.choice("person_id", "Person", person_choice, required=True))
    fields += [
        forms.choice("code_id", "Code", code_choices or [], required=True),
        forms.date("start_date", "Start date", required=True),
        forms.date("end_date", "End date", required=True),
        forms.time_("start_time", "Start time"),
        forms.time_("end_time", "End time"),
        forms.text("reason", "Reason"),
        forms.multiline("notes", "Notes"),
    ]
    return fields


class AbsencesScreen(Screen):
    title = "Absences"

    def refresh(self, tab: str = "list", cal_start: str | None = None, **_):
        self.clear()
        bar = self.header("Absences")
        ttk.Button(
            bar, text="+ Add absence", style="Accent.TButton", command=self._add_absence
        ).pack(side="right")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        listing = ttk.Frame(nb, style="TFrame", padding=12)
        calendar = ttk.Frame(nb, style="TFrame", padding=12)
        nb.add(listing, text="List")
        nb.add(calendar, text="Calendar")
        self._build_list(listing)
        self._build_calendar(calendar, cal_start)
        nb.select(1 if tab == "calendar" else 0)

    def _build_list(self, parent):
        rows = read(Q.absence_list())
        if not rows:
            empty_state(parent, "No absences recorded.").pack(anchor="w")
            return
        ttk.Label(
            parent, text="Double-click a row to edit.", style="Muted.TLabel"
        ).pack(anchor="w", pady=(0, 4))
        table = SearchableTree(
            parent,
            columns=[
                ("name", "Name", 200),
                ("code", "Code", 90),
                ("start", "Start", 110),
                ("end", "End", 110),
                ("kind", "Type", 90),
                ("reason", "Reason", 280),
            ],
            on_open=lambda iid: self._edit_absence(int(iid)),
            search_label="Filter",
        )
        table.pack(fill="both", expand=True)
        table.set_rows(
            [
                {
                    "_id": r.id,
                    "name": r.person_name,
                    "code": r.code,
                    "start": r.start_date.isoformat(),
                    "end": r.end_date.isoformat(),
                    "kind": "partial" if r.partial else "full",
                    "reason": r.reason or "",
                }
                for r in rows
            ]
        )

    # -- Write actions ----------------------------------------------------
    def _add_absence(self):
        people = read(commands.active_people_choices())
        codes = read(commands.absence_code_choices())
        vals = forms.prompt(
            self,
            "Add absence",
            _absence_fields(person_choice=people, code_choices=codes),
            submit_label="Add",
        )
        if not vals:
            return
        run_write(
            self,
            commands.create_absence(**vals),
            pii_texts=(vals.get("reason"), vals.get("notes")),
            on_done=lambda: self.app.show("absences", tab="list"),
        )

    def _edit_absence(self, absence_id: int):
        initial = read(Q.absence_get(absence_id))
        if not initial:
            return
        codes = read(commands.absence_code_choices())
        vals = forms.prompt(
            self,
            f"Edit absence — {initial['person_name']}",
            _absence_fields(code_choices=codes),
            initial=initial,
            submit_label="Save",
        )
        if vals is None:
            return
        run_write(
            self,
            commands.update_absence(absence_id, **vals),
            pii_texts=(vals.get("reason"), vals.get("notes")),
            confirm=None,
            on_done=lambda: self.app.show("absences", tab="list"),
        )

    def _build_calendar(self, parent, cal_start):
        start = date.fromisoformat(cal_start) if cal_start else date.today()
        data = read(Q.absence_calendar(start, 28))

        nav = ttk.Frame(parent)
        nav.pack(fill="x", pady=(0, 8))
        ttk.Button(
            nav,
            text="‹ Earlier",
            command=lambda: self.app.show(
                "absences", tab="calendar", cal_start=data.prev_start
            ),
        ).pack(side="left", padx=2)
        ttk.Button(
            nav, text="Today", command=lambda: self.app.show("absences", tab="calendar")
        ).pack(side="left", padx=2)
        ttk.Button(
            nav,
            text="Later ›",
            command=lambda: self.app.show(
                "absences", tab="calendar", cal_start=data.next_start
            ),
        ).pack(side="left", padx=2)
        ttk.Label(
            nav,
            text=f"{data.start_date.isoformat()} + {len(data.days)} days",
            style="Muted.TLabel",
        ).pack(side="right")

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

        small = theme.Fonts().small
        NAME_W = 22

        # Month band
        tk.Label(grid, text="", bg=theme.PANEL3, width=NAME_W).grid(
            row=0, column=0, sticky="nsew", padx=1, pady=1
        )
        col = 1
        for label, span in data.month_groups:
            tk.Label(
                grid,
                text=label,
                bg=theme.PANEL3,
                fg=theme.TEXT_DIM,
                font=theme.Fonts().bold,
                pady=2,
            ).grid(row=0, column=col, columnspan=span, sticky="nsew", padx=1, pady=1)
            col += span

        # Day-number header
        tk.Label(
            grid,
            text="Name",
            bg=theme.PANEL3,
            fg=theme.TEXT_DIM,
            anchor="w",
            font=theme.Fonts().bold,
            padx=8,
            width=NAME_W,
        ).grid(row=1, column=0, sticky="nsew", padx=1, pady=1)
        for c, d in enumerate(data.days, start=1):
            wd = d.strftime("%a")[0]
            weekend = d.weekday() >= 5
            tk.Label(
                grid,
                text=f"{wd}\n{d.day}",
                bg=(theme.BG2 if weekend else theme.PANEL3),
                fg=theme.TEXT_DIM,
                font=small,
                width=3,
            ).grid(row=1, column=c, sticky="nsew", padx=1, pady=1)

        # Person rows
        for r, crow in enumerate(data.rows, start=2):
            bgc = theme.PANEL2 if r % 2 else theme.PANEL
            tk.Label(
                grid,
                text=crow.name,
                bg=bgc,
                fg=theme.TEXT,
                anchor="w",
                font=small,
                padx=8,
                width=NAME_W,
            ).grid(row=r, column=0, sticky="nsew", padx=1, pady=1)
            for c, cell in enumerate(crow.cells, start=1):
                if cell.code:
                    status = "in_progress" if cell.partial else "dinq"
                    fg, cbg = theme.status_colors(status)
                    text = cell.code[:3]
                else:
                    fg, cbg, text = theme.MUTED, bgc, ""
                lab = tk.Label(grid, text=text, bg=cbg, fg=fg, font=small, width=3)
                lab.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)

        # Daily totals footer
        foot = len(data.rows) + 2
        tk.Label(
            grid,
            text="% present",
            bg=theme.PANEL3,
            fg=theme.TEXT_DIM,
            anchor="w",
            font=theme.Fonts().bold,
            padx=8,
            width=NAME_W,
        ).grid(row=foot, column=0, sticky="nsew", padx=1, pady=1)
        for c, pct in enumerate(data.daily_percent_present, start=1):
            col = theme.GOOD if pct >= 80 else theme.WARN if pct >= 60 else theme.BAD
            tk.Label(
                grid, text=f"{pct:g}", bg=theme.PANEL3, fg=col, font=small, width=3
            ).grid(row=foot, column=c, sticky="nsew", padx=1, pady=1)
