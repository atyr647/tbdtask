"""Personnel — Active (grouped) / Incoming / Departed tabs, plus profile."""

from __future__ import annotations

from tkinter import ttk

from .. import theme
from ..context import read
from ..widgets import Card, SearchableTree, StatTile, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen

_GROUP_ORDER = ["Leadership", "Senior", "Professional", "Associate", "Other"]


class PersonnelScreen(Screen):
    title = "Personnel"

    def refresh(self, person_id: int | None = None, tab: str = "active", **_):
        self.clear()
        if person_id is not None:
            self._show_profile(int(person_id))
            return

        self.header("Personnel")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        active = ttk.Frame(nb, style="TFrame", padding=12)
        incoming = ttk.Frame(nb, style="TFrame", padding=12)
        departed = ttk.Frame(nb, style="TFrame", padding=12)
        nb.add(active, text="Active")
        nb.add(incoming, text="Incoming")
        nb.add(departed, text="Departed")

        self._build_active(active)
        self._build_incoming(incoming)
        self._build_departed(departed)

        order = {"active": 0, "incoming": 1, "departed": 2}
        nb.select(order.get(tab, 0))

    # -- Active -----------------------------------------------------------
    def _build_active(self, parent):
        grouped = read(Q.personnel_active())
        total = sum(len(v) for v in grouped.values())
        rows = []
        for g in _GROUP_ORDER:
            for p in grouped.get(g, []):
                rows.append({
                    "_id": p.id,
                    "name": p.name,
                    "rate": p.rate or "",
                    "group": p.group,
                    "ds": p.duty_section if p.duty_section is not None else "",
                    "position": p.position or "",
                })
        ttk.Label(parent, text=f"{total} active personnel", style="Muted.TLabel"
                  ).pack(anchor="w", pady=(0, 6))
        table = SearchableTree(
            parent,
            columns=[("name", "Name", 220), ("rate", "Rate", 90),
                     ("group", "Group", 130), ("ds", "Duty", 60),
                     ("position", "Position", 200)],
            on_open=lambda iid: self.app.show("personnel", person_id=int(iid)),
            search_label="Filter roster",
        )
        table.pack(fill="both", expand=True)
        table.set_rows(rows)

    # -- Incoming ---------------------------------------------------------
    def _build_incoming(self, parent):
        rows = read(Q.personnel_incoming())
        if not rows:
            empty_state(parent, "No incoming personnel.").pack(anchor="w")
            return
        scroller = VScroll(parent)
        scroller.pack(fill="both", expand=True)
        steps = [("Orders", "orders_received"), ("Itinerary", "itinerary_received"),
                 ("AOB", "aob_scheduled"), ("Barracks", "barracks_assigned")]
        for r in rows:
            card = Card(scroller.body)
            card.pack(fill="x", pady=4)
            top = ttk.Frame(card.body, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(top, text=r.name, style="Card.TLabel",
                      font=theme.Fonts().bold).pack(side="left")
            arr = r.arrival_date.strftime("%d %b %Y") if r.arrival_date else "arrival TBD"
            ttk.Label(top, text=arr, style="CardMuted.TLabel").pack(side="right")
            chk = ttk.Frame(card.body, style="Card.TFrame")
            chk.pack(fill="x", pady=(6, 0))
            for label, attr in steps:
                done = getattr(r, attr)
                badge(chk, ("✓ " if done else "○ ") + label,
                      status="qualified" if done else "not_assigned").pack(
                    side="left", padx=(0, 6))
            meta = f"{r.checklist_done}/{r.checklist_total} in-processing"
            if r.sponsor_label:
                meta += f"   ·   Sponsor: {r.sponsor_label}"
            ttk.Label(card.body, text=meta, style="CardMuted.TLabel").pack(
                anchor="w", pady=(6, 0))

    # -- Departed ---------------------------------------------------------
    def _build_departed(self, parent):
        rows = read(Q.personnel_departed())
        if not rows:
            empty_state(parent, "No departed personnel.").pack(anchor="w")
            return
        table = SearchableTree(
            parent,
            columns=[("name", "Name", 220), ("rate", "Rate", 90),
                     ("departed", "Departed", 130), ("reason", "Reason", 300)],
            search_label="Filter",
        )
        table.pack(fill="both", expand=True)
        table.set_rows([
            {"_id": r.id, "name": r.name, "rate": r.rate or "",
             "departed": r.departed_at.strftime("%d %b %Y") if r.departed_at else "",
             "reason": r.reason or ""}
            for r in rows
        ])

    # -- Profile ----------------------------------------------------------
    def _show_profile(self, person_id: int):
        prof = read(Q.person_profile(person_id))
        if prof is None:
            self.header("Person not found")
            ttk.Button(self, text="← Back to personnel",
                       command=lambda: self.app.show("personnel")).pack(anchor="w")
            return

        bar = self.header(prof.name, None if prof.active else "Departed")
        ttk.Button(bar, text="← Back",
                   command=lambda: self.app.show("personnel")).pack(side="right")

        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        body = scroller.body

        # Stat row
        stats = ttk.Frame(body)
        stats.pack(fill="x", pady=(0, 12))
        for i, (val, cap) in enumerate([
            (prof.total_completed, "tasks completed (180d)"),
            (f"{prof.total_hours:g}", "hours logged"),
            (prof.poic_count, "as POIC"),
            (len(prof.quals), "qualifications"),
        ]):
            StatTile(stats, val, cap).grid(row=0, column=i, sticky="ew",
                                           padx=(0 if i == 0 else 8, 0))
            stats.columnconfigure(i, weight=1)

        cols = ttk.Frame(body)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="p")
        cols.columnconfigure(1, weight=1, uniform="p")

        # Left: current standing (effective-dated rows, most recent first)
        left = ttk.Frame(cols)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._eff_card(left, "Rate / paygrade", prof.rates)
        self._eff_card(left, "Duty section", prof.duty_sections)
        self._eff_card(left, "PRD history", prof.prds)
        self._eff_card(left, "Driver's license", prof.drivers_licenses)

        # Right: quals + work breakdown
        right = ttk.Frame(cols)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        qcard = Card(right, title="Qualifications")
        qcard.pack(fill="x", pady=(0, 8))
        if not prof.quals:
            empty_state(qcard.body, "None assigned.").pack(anchor="w")
        for q in prof.quals:
            row = ttk.Frame(qcard.body, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            badge(row, q.status.replace("_", " "), status=q.status).pack(side="left")
            ttk.Label(row, text=f"  {q.name}", style="Card.TLabel").pack(side="left")

        wcard = Card(right, title="Work by category (180 days)")
        wcard.pack(fill="x")
        if not prof.by_category:
            empty_state(wcard.body, "No completed tasks in window.").pack(anchor="w")
        for c in prof.by_category:
            row = ttk.Frame(wcard.body, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=c.name, style="Card.TLabel").pack(side="left")
            detail = f"{c.count} task(s), {c.hours:g}h"
            if c.poic_count:
                detail += f", {c.poic_count} POIC"
            ttk.Label(row, text=detail, style="CardMuted.TLabel").pack(side="right")

        if prof.notes:
            ncard = Card(body, title="Notes")
            ncard.pack(fill="x", pady=(12, 0))
            ttk.Label(ncard.body, text=prof.notes, style="Card.TLabel",
                      wraplength=900, justify="left").pack(anchor="w")

    def _eff_card(self, parent, title, rows):
        card = Card(parent, title=title)
        card.pack(fill="x", pady=(0, 8))
        if not rows:
            empty_state(card.body, "—").pack(anchor="w")
            return
        for i, r in enumerate(rows):
            row = ttk.Frame(card.body, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            is_current = r.valid_to is None
            lbl = ttk.Label(row, text=r.label, style="Card.TLabel")
            if is_current:
                lbl.configure(font=theme.Fonts().bold)
            lbl.pack(side="left")
            span = f"from {r.valid_from.isoformat()}"
            if r.valid_to:
                span += f" to {r.valid_to.isoformat()}"
            else:
                span = "current · " + span
            ttk.Label(row, text=span, style="CardMuted.TLabel").pack(side="right")
