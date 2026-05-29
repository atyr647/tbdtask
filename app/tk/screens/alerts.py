"""Alerts — PRD windows, qual expiry, pending carry-overs."""

from __future__ import annotations

from tkinter import ttk

from .. import theme
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
        ttk.Button(toggle, text="Active",
                   style="Accent.TButton" if show == "active" else "TButton",
                   command=lambda: self.app.show("alerts", show="active")).pack(side="left")
        ttk.Button(toggle, text="History",
                   style="Accent.TButton" if show == "history" else "TButton",
                   command=lambda: self.app.show("alerts", show="history")).pack(
            side="left", padx=(4, 0))

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
            ttk.Label(top, text=f"  {a.type_label}", style="Card.TLabel",
                      font=theme.Fonts().bold).pack(side="left")
            if a.person_label:
                ttk.Label(top, text=a.person_label, style="CardMuted.TLabel").pack(
                    side="right")
            meta = []
            if a.detail:
                meta.append(a.detail)
            if a.snoozed_until:
                meta.append(f"snoozed until {a.snoozed_until.isoformat()}")
            if a.resolved_at:
                meta.append(f"resolved {a.resolved_at.strftime('%d %b %Y')}")
            if meta:
                ttk.Label(card.body, text=" · ".join(meta),
                          style="CardMuted.TLabel").pack(anchor="w", pady=(4, 0))
