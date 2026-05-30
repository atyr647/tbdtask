"""Recurring task templates — list + create / edit / archive.

A TaskTemplate is the originating definition for recurring tasks; the
generator materialises them onto each new worklist. This screen mirrors
the web app's templates CRUD, including the recurrence builder (daily /
weekday set / every-N-weeks / day-of-month / Nth-weekday).
"""

from __future__ import annotations

from tkinter import ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import Card, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


class TemplatesScreen(Screen):
    title = "Recurring"

    def refresh(self, **_):
        self.clear()
        bar = self.header("Recurring tasks")
        ttk.Button(
            bar, text="+ New template", style="Accent.TButton", command=self._add
        ).pack(side="right")
        rows = read(Q.template_list())
        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        if not rows:
            empty_state(scroller.body, "No recurring templates yet.").pack(anchor="w")
            return
        for t in rows:
            card = Card(scroller.body)
            card.pack(fill="x", pady=3)
            top = ttk.Frame(card.body, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(
                top, text=t["name"], style="Card.TLabel", font=theme.Fonts().bold
            ).pack(side="left")
            badge(top, t["recurrence"], status="recurring").pack(side="left", padx=8)
            ttk.Button(
                top, text="Archive", command=lambda tid=t["id"]: self._archive(tid)
            ).pack(side="right")
            ttk.Button(
                top, text="Edit", command=lambda tid=t["id"]: self._edit(tid)
            ).pack(side="right", padx=4)
            meta = []
            if t["category"]:
                meta.append(t["category"])
            if t["estimated_hours"]:
                meta.append(f"{t['estimated_hours']:g}h est")
            meta.append(f"carry: {t['carry_over_policy']}")
            ttk.Label(card.body, text=" · ".join(meta), style="CardMuted.TLabel").pack(
                anchor="w", pady=(4, 0)
            )

    # -- form -------------------------------------------------------------
    def _fields(self):
        cats = read(commands.task_categories_choices())
        quals = read(commands.qual_choices())
        return [
            forms.text("name", "Name", required=True),
            forms.choice("category_id", "Category", cats),
            forms.multiline("description", "Description"),
            forms.number("estimated_hours", "Estimated hours"),
            forms.choice(
                "carry_over_policy",
                "Carry-over",
                [(v, lbl) for v, lbl in commands.CARRY_OVER_POLICIES],
            ),
            forms.choice(
                "rec_kind",
                "Recurrence",
                [(v, lbl) for v, lbl in commands.RECURRENCE_KINDS],
            ),
            forms.text(
                "rec_weekdays",
                "  Weekdays (0=Mon..6=Sun, comma-sep)",
                help="for 'weekday(s)'",
            ),
            forms.integer("rec_n", "  N (weeks, or Nth)", help="for N-weeks/Nth"),
            forms.integer("rec_weekday", "  Weekday (0=Mon)", help="for N-weeks/Nth"),
            forms.integer("rec_day", "  Day of month", help="for day-of-month"),
            forms.date("rec_anchor", "  Anchor date", help="for N-weeks"),
            forms.multichoice("required_quals", "Required quals", quals),
            forms.boolean("required_drivers_license", "Requires driver's license"),
            forms.integer("required_duty_section", "Required duty section"),
            forms.multiline("notes", "Notes"),
        ]

    def _recurrence_from(self, vals):
        kind = vals.get("rec_kind") or "none"
        weekdays = []
        raw = (vals.get("rec_weekdays") or "").strip()
        if raw:
            for part in raw.replace(" ", "").split(","):
                if part.isdigit():
                    weekdays.append(int(part))
        return commands._build_recurrence(
            kind,
            weekdays=weekdays,
            n=vals.get("rec_n"),
            weekday=vals.get("rec_weekday"),
            day=vals.get("rec_day"),
            anchor=vals.get("rec_anchor"),
        )

    def _add(self):
        vals = forms.prompt(
            self,
            "New recurring template",
            self._fields(),
            initial={"carry_over_policy": "auto_same_person", "rec_kind": "none"},
            submit_label="Create",
        )
        if not vals:
            return
        run_write(
            self,
            commands.create_template(
                name=vals["name"],
                category_id=vals.get("category_id"),
                description=vals.get("description"),
                estimated_hours=vals.get("estimated_hours"),
                carry_over_policy=vals.get("carry_over_policy") or "auto_same_person",
                recurrence=self._recurrence_from(vals),
                required_drivers_license=vals.get("required_drivers_license"),
                required_duty_section=vals.get("required_duty_section"),
                required_quals=vals.get("required_quals") or [],
                notes=vals.get("notes"),
            ),
            pii_texts=(vals.get("description"), vals.get("notes")),
            on_done=self.refresh,
        )

    def _edit(self, template_id):
        initial = read(Q.template_get(template_id))
        if not initial:
            return
        # Render the stored weekday list back into the comma-separated field.
        initial = dict(initial)
        initial["rec_weekdays"] = ",".join(
            str(x) for x in (initial.get("rec_weekdays") or [])
        )
        vals = forms.prompt(
            self, "Edit template", self._fields(), initial=initial, submit_label="Save"
        )
        if not vals:
            return
        run_write(
            self,
            commands.update_template(
                template_id,
                name=vals["name"],
                category_id=vals.get("category_id"),
                description=vals.get("description"),
                estimated_hours=vals.get("estimated_hours"),
                carry_over_policy=vals.get("carry_over_policy") or "auto_same_person",
                recurrence=self._recurrence_from(vals),
                required_drivers_license=vals.get("required_drivers_license"),
                required_duty_section=vals.get("required_duty_section"),
                required_quals=vals.get("required_quals") or [],
                notes=vals.get("notes"),
            ),
            pii_texts=(vals.get("description"), vals.get("notes")),
            on_done=self.refresh,
        )

    def _archive(self, template_id):
        vals = forms.prompt(
            self,
            "Archive template",
            [forms.text("reason", "Reason")],
            submit_label="Archive",
        )
        if vals is None:
            return
        run_write(
            self,
            commands.archive_template(template_id, vals.get("reason") or ""),
            confirm=(
                "Archive template",
                "Stop generating this recurring task on new worklists?",
            ),
            on_done=self.refresh,
        )
