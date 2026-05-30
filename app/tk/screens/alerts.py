"""Alerts — PRD windows, qual expiry, pending carry-overs."""

from __future__ import annotations

from tkinter import ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import Card, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


class AlertsScreen(Screen):
    title = "Alerts"

    def refresh(self, show: str = "active", **_):
        self.clear()
        bar = self.header("Alerts")
        toggle = ttk.Frame(bar)
        toggle.pack(side="right")
        ttk.Button(
            toggle,
            text="Active",
            style="Accent.TButton" if show == "active" else "TButton",
            command=lambda: self.app.show("alerts", show="active"),
        ).pack(side="left")
        ttk.Button(
            toggle,
            text="History",
            style="Accent.TButton" if show == "history" else "TButton",
            command=lambda: self.app.show("alerts", show="history"),
        ).pack(side="left", padx=(4, 0))

        rows = read(Q.alerts_list(show))
        if not rows:
            empty_state(self, "Nothing here — all clear.").pack(anchor="w")
            return

        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        for a in rows:
            card = Card(scroller.body)
            card.pack(fill="x", pady=3)
            top = ttk.Frame(card.body, style="Card.TFrame")
            top.pack(fill="x")
            badge(top, a.severity.upper(), severity=a.severity).pack(side="left")
            ttk.Label(
                top,
                text=f"  {a.type_label}",
                style="Card.TLabel",
                font=theme.Fonts().bold,
            ).pack(side="left")
            if a.person_label:
                ttk.Label(top, text=a.person_label, style="CardMuted.TLabel").pack(
                    side="right"
                )
            meta = []
            if a.detail:
                meta.append(a.detail)
            if a.snoozed_until:
                meta.append(f"snoozed until {a.snoozed_until.isoformat()}")
            if a.resolved_at:
                meta.append(f"resolved {a.resolved_at.strftime('%d %b %Y')}")
            if meta:
                ttk.Label(
                    card.body, text=" · ".join(meta), style="CardMuted.TLabel"
                ).pack(anchor="w", pady=(4, 0))
            if show == "active":
                self._action_bar(card.body, a)

    def _refresh(self, show):
        return lambda: self.app.show("alerts", show=show)

    def _action_bar(self, parent, a):
        acts = ttk.Frame(parent, style="Card.TFrame")
        acts.pack(fill="x", pady=(8, 0))
        ttk.Button(
            acts,
            text="Dismiss",
            command=lambda: run_write(
                self, commands.dismiss_alert(a.id), on_done=self._refresh("active")
            ),
        ).pack(side="left")
        ttk.Button(acts, text="Snooze", command=lambda: self._snooze(a.id)).pack(
            side="left", padx=4
        )
        ttk.Button(acts, text="Resolve", command=lambda: self._resolve(a.id)).pack(
            side="left", padx=4
        )
        if a.is_prd and a.has_person:
            ttk.Button(
                acts,
                text="Update PRD",
                style="Accent.TButton",
                command=lambda: self._extend_prd(a.id),
            ).pack(side="left", padx=4)
            ttk.Button(
                acts,
                text="Archive person",
                command=lambda: self._archive(a.id, a.person_label),
            ).pack(side="left", padx=4)

    def _snooze(self, alert_id):
        vals = forms.prompt(
            self,
            "Snooze alert",
            [forms.date("until", "Snooze until", required=True)],
            submit_label="Snooze",
        )
        if not vals:
            return
        run_write(
            self,
            commands.snooze_alert(alert_id, vals["until"]),
            on_done=self._refresh("active"),
        )

    def _resolve(self, alert_id):
        vals = forms.prompt(
            self,
            "Resolve alert",
            [forms.text("note", "Note (optional)")],
            submit_label="Resolve",
        )
        if vals is None:
            return
        run_write(
            self,
            commands.resolve_alert(alert_id, vals.get("note")),
            pii_texts=(vals.get("note"),),
            on_done=self._refresh("active"),
        )

    def _extend_prd(self, alert_id):
        vals = forms.prompt(
            self,
            "Update planned rotation date",
            [
                forms.date("new_date", "New PRD"),
                forms.integer("days", "…or extend by N days"),
            ],
            submit_label="Update",
        )
        if not vals:
            return
        if not vals.get("new_date") and not vals.get("days"):
            return
        run_write(
            self,
            commands.extend_prd(
                alert_id, new_date=vals.get("new_date"), days=vals.get("days")
            ),
            on_done=self._refresh("active"),
        )

    def _archive(self, alert_id, name):
        run_write(
            self,
            commands.archive_person_from_alert(alert_id),
            confirm=(
                "Archive person",
                f"Move {name or 'this person'} to departed and resolve the alert?",
            ),
            on_done=self._refresh("active"),
        )
