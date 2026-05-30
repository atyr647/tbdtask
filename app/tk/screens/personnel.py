"""Personnel — Active (grouped) / Incoming / Departed tabs, plus profile."""

from __future__ import annotations

from tkinter import messagebox, ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import Card, SearchableTree, StatTile, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


def _person_fields(incoming: bool, sponsor_choices=None):
    """Field spec shared by the add/edit person forms.

    Paygrade is picked first; the Rating field only shows for enlisted
    grades (warrant/officer have no rating). Position is an editable Navy
    billet dropdown that also accepts custom text.
    """
    fields = [
        forms.text("last_name", "Last name", required=True),
        forms.text("first_name", "First name"),
        forms.choice("paygrade", "Paygrade", commands.paygrade_choices()),
        forms.choice("rating", "Rating", commands.rating_choices(),
                     visible_when=("paygrade", commands.is_enlisted_paygrade),
                     help="enlisted only"),
        forms.combo("position", "Position / billet", commands.position_choices(),
                    help="pick or type"),
    ]
    if incoming:
        fields += [
            forms.date("arrival_date", "Arrival date"),
            forms.choice("sponsor_person_id", "Sponsor", sponsor_choices or []),
            forms.boolean("orders_received", "Orders received"),
            forms.boolean("itinerary_received", "Itinerary received"),
            forms.boolean("aob_scheduled", "AOB scheduled"),
            forms.boolean("barracks_assigned", "Barracks assigned"),
        ]
    else:
        fields += [
            forms.choice("duty_section", "Duty section",
                         [(d, str(d)) for d in commands.DUTY_SECTIONS]),
            forms.date("prd_date", "PRD"),
            forms.boolean("has_drivers_license", "Has driver's license"),
            forms.date("drivers_license_expires", "License expires"),
        ]
    fields.append(forms.multiline("notes", "Notes"))
    return fields


# Mess grouping order for the active roster.
_GROUP_ORDER = list(commands.rank_catalog.GROUP_ORDER)


class PersonnelScreen(Screen):
    title = "Personnel"

    def refresh(self, person_id: int | None = None, tab: str = "active", **_):
        self.clear()
        if person_id is not None:
            self._show_profile(int(person_id))
            return

        bar = self.header("Personnel")
        actions = ttk.Frame(bar)
        actions.pack(side="right")
        ttk.Button(actions, text="+ Add person", style="Accent.TButton",
                   command=self._add_person).pack(side="left", padx=2)
        ttk.Button(actions, text="+ Incoming",
                   command=self._add_incoming).pack(side="left", padx=2)

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
            acts = ttk.Frame(card.body, style="Card.TFrame")
            acts.pack(fill="x", pady=(6, 0))
            ttk.Button(acts, text="Edit checklist",
                       command=lambda pid=r.id: self._edit_checklist(pid)).pack(
                side="left")
            ttk.Button(acts, text="Mark arrived", style="Accent.TButton",
                       command=lambda pid=r.id, nm=r.name:
                       self._mark_arrived(pid, nm)).pack(side="left", padx=4)

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
        if prof.active:
            ttk.Button(bar, text="Edit", style="Accent.TButton",
                       command=lambda: self._edit_person(person_id)).pack(
                side="right", padx=4)
            ttk.Button(bar, text="Add absence",
                       command=lambda: self._add_absence(person_id)).pack(
                side="right", padx=4)
            ttk.Button(bar, text="Move to departed",
                       command=lambda: self._depart(person_id, prof.name)).pack(
                side="right", padx=4)

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
        qcard = Card(right)
        qcard.pack(fill="x", pady=(0, 8))
        qhead = ttk.Frame(qcard.body, style="Card.TFrame")
        qhead.pack(fill="x", pady=(0, 6))
        ttk.Label(qhead, text="Qualifications", style="CardH2.TLabel").pack(side="left")
        if prof.active:
            ttk.Button(qhead, text="+ Assign",
                       command=lambda: self._assign_quals(person_id)).pack(side="right")
        if not prof.quals:
            empty_state(qcard.body, "None assigned.").pack(anchor="w")
        for q in prof.quals:
            row = ttk.Frame(qcard.body, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            badge(row, q.status.replace("_", " "), status=q.status).pack(side="left")
            lbl = ttk.Label(row, text=f"  {q.name}", style="Card.TLabel")
            lbl.pack(side="left")
            if q.expires_at:
                ttk.Label(row, text=f"exp {q.expires_at.strftime('%Y-%m-%d')}",
                          style="CardMuted.TLabel").pack(side="right")
            if prof.active and q.pq_id is not None:
                lbl.configure(cursor="hand2")
                lbl.bind("<Button-1>",
                         lambda e, q=q: self._edit_qual(person_id, q))

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

    # -- Write actions ----------------------------------------------------
    def _add_person(self):
        vals = forms.prompt(self, "Add person", _person_fields(incoming=False),
                            submit_label="Create")
        if not vals:
            return
        run_write(
            self, commands.create_person(**vals),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("personnel", tab="active"),
        )

    def _add_incoming(self):
        sponsors = read(commands.active_people_choices())
        vals = forms.prompt(self, "Add incoming personnel",
                            _person_fields(incoming=True, sponsor_choices=sponsors),
                            submit_label="Create")
        if not vals:
            return
        run_write(
            self, commands.create_incoming(**vals),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("personnel", tab="incoming"),
        )

    def _edit_person(self, person_id: int):
        initial = read(Q.person_current(person_id))
        if not initial:
            return
        vals = forms.prompt(self, "Edit person", _person_fields(incoming=False),
                            initial=initial, submit_label="Save")
        if not vals:
            return
        run_write(
            self, commands.update_person(person_id, **vals),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("personnel", person_id=person_id),
        )

    def _depart(self, person_id: int, name: str):
        vals = forms.prompt(self, f"Move {name} to departed",
                            [forms.text("reason", "Reason")], submit_label="Confirm")
        if vals is None:
            return
        run_write(
            self, commands.archive_person(person_id, vals.get("reason") or ""),
            pii_texts=(vals.get("reason"),),
            on_done=lambda: self.app.show("personnel", tab="active"),
        )

    def _add_absence(self, person_id: int):
        codes = read(commands.absence_code_choices())
        vals = forms.prompt(self, "Add absence", [
            forms.choice("code_id", "Code", codes, required=True),
            forms.date("start_date", "Start date", required=True),
            forms.date("end_date", "End date", required=True),
            forms.time_("start_time", "Start time"),
            forms.time_("end_time", "End time"),
            forms.text("reason", "Reason"),
            forms.multiline("notes", "Notes"),
        ], submit_label="Add")
        if not vals:
            return
        run_write(
            self, commands.create_absence(person_id=person_id, **vals),
            pii_texts=(vals.get("reason"), vals.get("notes")),
            on_done=lambda: self.app.show("personnel", person_id=person_id),
        )

    def _edit_checklist(self, person_id: int):
        initial = read(Q.person_current(person_id))
        if not initial:
            return
        sponsors = read(commands.active_people_choices())
        vals = forms.prompt(self, "In-processing checklist", [
            forms.date("arrival_date", "Arrival date"),
            forms.choice("sponsor_person_id", "Sponsor", sponsors),
            forms.boolean("orders_received", "Orders received"),
            forms.boolean("itinerary_received", "Itinerary received"),
            forms.boolean("aob_scheduled", "AOB scheduled"),
            forms.boolean("barracks_assigned", "Barracks assigned"),
        ], initial=initial, submit_label="Save")
        if not vals:
            return
        run_write(
            self, commands.update_checklist(person_id, **vals),
            on_done=lambda: self.app.show("personnel", tab="incoming"),
        )

    def _mark_arrived(self, person_id: int, name: str):
        run_write(
            self, commands.mark_arrived(person_id),
            confirm=("Mark arrived", f"Move {name} to the active roster?"),
            on_done=lambda: self.app.show("personnel", tab="incoming"),
        )

    def _assign_quals(self, person_id: int):
        choices = read(commands.qual_choices(exclude_person_id=person_id))
        if not choices:
            messagebox.showinfo(
                "No quals to assign",
                "This person already has a current record for every "
                "qualification in the catalog.", parent=self)
            return
        vals = forms.prompt(self, "Assign qualifications", [
            forms.multichoice("qual_ids", "Qualifications", choices),
            forms.choice("status", "Status",
                         [(s, s.replace("_", " ")) for s in
                          commands.PERSON_QUAL_STATUSES]),
            forms.date("started_at", "Started"),
            forms.date("achieved_at", "Achieved"),
            forms.multiline("notes", "Notes"),
        ], initial={"status": "assigned"}, submit_label="Assign")
        if not vals:
            return
        if not vals.get("qual_ids"):
            return
        run_write(
            self, commands.assign_quals(
                person_id, qual_ids=vals["qual_ids"],
                status=vals.get("status") or "assigned",
                started_at=vals.get("started_at"),
                achieved_at=vals.get("achieved_at"),
                notes=vals.get("notes")),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("personnel", person_id=person_id))

    def _edit_qual(self, person_id: int, q):
        initial = {
            "status": q.status,
            "started_at": q.started_at.strftime("%Y-%m-%d") if q.started_at else None,
            "achieved_at": q.achieved_at.strftime("%Y-%m-%d") if q.achieved_at else None,
            "notes": q.notes,
        }
        vals = forms.prompt(self, f"Update — {q.name}", [
            forms.choice("status", "Status",
                         [(s, s.replace("_", " ")) for s in
                          commands.PERSON_QUAL_STATUSES], required=True),
            forms.date("started_at", "Started"),
            forms.date("achieved_at", "Achieved"),
            forms.multiline("notes", "Notes"),
            forms.date("effective_date", "Effective date"),
        ], initial=initial, submit_label="Save")
        if not vals:
            return
        run_write(
            self, commands.update_person_qual(
                person_id, q.pq_id,
                status=vals.get("status") or q.status,
                started_at=vals.get("started_at"),
                achieved_at=vals.get("achieved_at"),
                notes=vals.get("notes"),
                effective_date=vals.get("effective_date")),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("personnel", person_id=person_id))
