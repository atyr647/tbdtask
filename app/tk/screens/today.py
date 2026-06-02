"""Today / day overview — who's out, percent present, and the day's tasks."""

from __future__ import annotations

from datetime import date

from tkinter import ttk

from .. import theme
from ..context import read
from ..widgets import Card, StatTile, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


class TodayScreen(Screen):
    title = "Today"

    def refresh(self, day_iso: str | None = None, **_):
        self.clear()
        on_date = date.fromisoformat(day_iso) if day_iso else date.today()
        data = read(Q.day_view(on_date))

        bar = self.header(on_date.strftime("%A · %d %B %Y"))
        nav = ttk.Frame(bar)
        nav.pack(side="right")
        ttk.Button(
            nav,
            text="‹ Prev",
            command=lambda: self.app.show("today", day_iso=data.prev_day),
        ).pack(side="left", padx=2)
        ttk.Button(nav, text="Today", command=lambda: self.app.show("today")).pack(
            side="left", padx=2
        )
        ttk.Button(
            nav,
            text="Next ›",
            command=lambda: self.app.show("today", day_iso=data.next_day),
        ).pack(side="left", padx=2)

        # Stat row
        stats = ttk.Frame(self)
        stats.pack(fill="x", pady=(0, 14))
        pct_color = (
            theme.GOOD
            if data.percent_present >= 80
            else theme.WARN
            if data.percent_present >= 60
            else theme.BAD
        )
        tiles = [
            (f"{data.percent_present:g}%", "present", pct_color),
            (data.present_full, "fully present", theme.TEXT),
            (
                data.full_absent,
                "out (full day)",
                theme.BAD if data.full_absent else theme.MUTED,
            ),
            (
                data.partial_absent,
                "partial day",
                theme.WARN if data.partial_absent else theme.MUTED,
            ),
            (data.total, "on roster", theme.TEXT),
        ]
        for i, (val, cap, col) in enumerate(tiles):
            StatTile(stats, val, cap, color=col).grid(
                row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0)
            )
            stats.columnconfigure(i, weight=1)

        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        cols = ttk.Frame(scroller.body)
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=1, uniform="c")
        cols.columnconfigure(1, weight=1, uniform="c")

        # Out today
        out_card = Card(cols, title=f"Out today ({len(data.absent_rows)})")
        out_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        if not data.absent_rows:
            empty_state(out_card.body, "Everyone present.").pack(anchor="w")
        else:
            for r in data.absent_rows:
                row = ttk.Frame(out_card.body, style="Card.TFrame")
                row.pack(fill="x", pady=2)
                badge(
                    row,
                    r.code or "OUT",
                    status="dinq" if not r.partial else "in_progress",
                ).pack(side="left")
                ttk.Label(row, text=f"  {r.name}", style="Card.TLabel").pack(
                    side="left"
                )
                extra = r.window_label or "full day"
                if r.reason:
                    extra += f" — {r.reason}"
                ttk.Label(row, text=extra, style="CardMuted.TLabel").pack(side="right")
        if data.by_code:
            codes = "   ".join(f"{k}: {v}" for k, v in sorted(data.by_code.items()))
            ttk.Label(out_card.body, text=codes, style="CardMuted.TLabel").pack(
                anchor="w", pady=(8, 0)
            )

        # Tasks
        task_card = Card(cols, title=f"Tasks ({len(data.tasks)})")
        task_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        if not data.tasks:
            empty_state(task_card.body, "No tasks scheduled.").pack(anchor="w")
        else:
            for t in data.tasks:
                row = ttk.Frame(task_card.body, style="Card.TFrame")
                row.pack(fill="x", pady=3)
                badge(row, t.status.replace("_", " "), status=t.status).pack(
                    side="left"
                )
                ttk.Label(row, text=f"  {t.name}", style="Card.TLabel").pack(side="left")
                if t.assignees:
                    ttk.Label(
                        row, text=", ".join(t.assignees[:3]), style="CardMuted.TLabel"
                    ).pack(side="right")
