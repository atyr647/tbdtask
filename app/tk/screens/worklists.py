"""Weekly worklists — list of weeks + the day-by-day week view."""

from __future__ import annotations

from tkinter import ttk

from .. import commands, forms, theme
from ..actions import run_write
from ..context import read
from ..widgets import Card, VScroll, badge, empty_state
from .. import queries as Q
from .base import Screen


class WorklistsScreen(Screen):
    title = "Worklists"

    def refresh(self, worklist_id: int | None = None, **_):
        self.clear()
        if worklist_id is not None:
            self._show_week(int(worklist_id))
            return

        bar = self.header("Worklists")
        data = read(Q.worklist_list())
        ttk.Button(bar, text="+ New worklist", style="Accent.TButton",
                   command=lambda: self._new_worklist(data.suggested_monday)).pack(
            side="right")
        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        self._section(scroller.body, "Current", data.current)
        self._section(scroller.body, "Upcoming", data.upcoming)
        self._section(scroller.body, "Archived", data.archived, archived=True)
        if not (data.current or data.upcoming or data.archived):
            empty_state(scroller.body, "No worklists yet.").pack(anchor="w")

    def _section(self, parent, title, rows, archived=False):
        if not rows:
            return
        ttk.Label(parent, text=title, style="H2.TLabel").pack(
            anchor="w", pady=(10, 4))
        for w in rows:
            card = Card(parent)
            card.pack(fill="x", pady=3)
            row = ttk.Frame(card.body, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Label(row, text=w.name, style="Card.TLabel",
                      font=theme.Fonts().bold).pack(side="left")
            if w.locked:
                badge(row, "locked", status="qualified").pack(side="left", padx=8)
            if w.version > 1:
                badge(row, f"v{w.version}", status="in_progress").pack(side="left")
            if w.amendment_count:
                ttk.Label(row, text=f"{w.amendment_count} amendment(s)",
                          style="CardMuted.TLabel").pack(side="left", padx=8)
            ttk.Label(row, text=f"week of {w.week_starting.strftime('%d %b %Y')}",
                      style="CardMuted.TLabel").pack(side="right")
            ttk.Button(row, text="Open",
                       command=lambda wid=w.id: self.app.show(
                           "worklists", worklist_id=wid)).pack(side="right", padx=8)

    def _show_week(self, worklist_id: int):
        view = read(Q.worklist_show(worklist_id))
        if view is None:
            self.header("Worklist not found")
            ttk.Button(self, text="← Back",
                       command=lambda: self.app.show("worklists")).pack(anchor="w")
            return

        bar = self.header(view.name,
                          f"week of {view.week_starting.strftime('%d %b %Y')}")
        ttk.Button(bar, text="← Back",
                   command=lambda: self.app.show("worklists")).pack(side="right")
        if view.locked:
            badge(bar, f"locked by {view.locked_by or '—'}", status="qualified"
                  ).pack(side="right", padx=8)
        else:
            ttk.Button(bar, text="Lock week", style="Accent.TButton",
                       command=lambda: self._lock(worklist_id)).pack(
                side="right", padx=8)
            ttk.Button(bar, text="+ Add task",
                       command=lambda: self._add_task(view)).pack(
                side="right", padx=4)
            ttk.Button(bar, text="Regenerate recurring",
                       command=lambda: self._regenerate(worklist_id)).pack(
                side="right", padx=4)
        self._locked = view.locked
        if view.pending_count:
            badge(bar, f"{view.pending_count} carry-over pending",
                  status="in_progress").pack(side="right", padx=8)
        ttk.Label(bar, text=f"{view.week_total_tasks} tasks",
                  style="Muted.TLabel").pack(side="right", padx=8, pady=(10, 0))

        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        for day in view.days:
            if not (day.persons or day.unassigned or day.out_today):
                continue
            title = (f"{day.weekday} · {day.on_date.strftime('%d %b')}  "
                     f"({day.percent_present:g}% present)")
            card = Card(scroller.body, title=title)
            card.pack(fill="x", pady=4)
            if day.out_today:
                out = ttk.Frame(card.body, style="Card.TFrame")
                out.pack(fill="x", pady=(0, 6))
                ttk.Label(out, text="Out:", style="CardMuted.TLabel").pack(side="left")
                for r in day.out_today:
                    badge(out, f"{r.code or 'OUT'} {r.name}",
                          status="dinq" if not r.partial else "in_progress").pack(
                        side="left", padx=3)
            for person in day.persons:
                self._person_row(card.body, person)
            for t in day.unassigned:
                row = ttk.Frame(card.body, style="Card.TFrame")
                row.pack(fill="x", pady=1)
                badge(row, "unassigned", status="open").pack(side="left")
                lbl = ttk.Label(row, text=f"  {t.name}", style="Card.TLabel")
                lbl.pack(side="left")
                self._bind_edit(lbl, t.id)

    def _person_row(self, parent, person):
        block = ttk.Frame(parent, style="Card.TFrame")
        block.pack(fill="x", pady=(4, 0))
        head = ttk.Frame(block, style="Card.TFrame")
        head.pack(fill="x")
        label = person.name
        if person.rate:
            label = f"{person.rate} {person.name}" if not label.startswith(
                person.rate) else label
        ttk.Label(head, text=label, style="Card.TLabel",
                  font=theme.Fonts().bold).pack(side="left")
        if person.duty_section is not None:
            ttk.Label(head, text=f"  DS{person.duty_section}",
                      style="CardMuted.TLabel").pack(side="left")
        if person.out_code:
            badge(head, person.out_code, status="dinq").pack(side="left", padx=6)
        for t in person.tasks:
            row = ttk.Frame(block, style="Card.TFrame")
            row.pack(fill="x", padx=(18, 0))
            badge(row, t.status.replace("_", " "), status=t.status).pack(side="left")
            text = f"  {t.name}"
            if t.is_poic:
                text += "  (POIC)"
            lbl = ttk.Label(row, text=text, style="Card.TLabel")
            lbl.pack(side="left")
            self._bind_edit(lbl, t.id)
            meta = []
            if t.category:
                meta.append(t.category)
            if t.other_assignees:
                meta.append("with " + ", ".join(t.other_assignees[:3]))
            if meta:
                ttk.Label(row, text=" · ".join(meta), style="CardMuted.TLabel"
                          ).pack(side="right")

    def _bind_edit(self, widget, task_id):
        """Make a task label clickable to open its edit form (unlocked weeks)."""
        if getattr(self, "_locked", False):
            return
        widget.configure(cursor="hand2")
        widget.bind("<Button-1>", lambda e: self._edit_task(task_id))

    def _lock(self, worklist_id):
        vals = forms.prompt(
            self, "Lock week",
            [forms.text("locked_by_name", "Locked by")],
            submit_label="Lock")
        if vals is None:
            return
        run_write(
            self,
            commands.lock_worklist(worklist_id, vals.get("locked_by_name")),
            confirm=("Lock week",
                     "Locking makes this week immutable. Changes afterwards "
                     "require an amendment. Continue?"),
            on_done=lambda: self.app.show("worklists", worklist_id=worklist_id),
        )

    # -- Write actions ----------------------------------------------------
    def _new_worklist(self, suggested_monday):
        vals = forms.prompt(self, "New worklist", [
            forms.date("week_starting", "Week starting (Monday)", required=True),
        ], initial={"week_starting": suggested_monday}, submit_label="Create")
        if not vals:
            return
        wid = run_write(self, commands.create_worklist(vals["week_starting"]))
        if wid:
            self.app.show("worklists", worklist_id=wid)

    def _regenerate(self, worklist_id):
        run_write(
            self, commands.generate_worklist_tasks(worklist_id),
            confirm=("Regenerate recurring tasks",
                     "Re-run recurring templates for this week? Existing "
                     "tasks are kept; only missing recurring ones are added."),
            on_done=lambda: self.app.show("worklists", worklist_id=worklist_id))

    def _task_fields(self, days, include_status=False):
        people = read(commands.active_people_choices())
        cats = read(commands.task_categories_choices())
        day_choices = [(d.on_date.isoformat(),
                        f"{d.weekday} · {d.on_date.strftime('%d %b')}")
                       for d in days]
        fields = [
            forms.text("name", "Task name", required=True),
            forms.choice("scheduled_date", "Day", day_choices),
            forms.choice("category_id", "Category", cats),
            forms.multiline("description", "Description"),
        ]
        if include_status:
            fields += [
                forms.choice("status", "Status",
                             [(s, s.replace("_", " ")) for s in commands.TASK_STATUSES]),
                forms.number("hours", "Hours"),
                forms.multiline("completion_notes", "Completion notes"),
            ]
        else:
            fields += [
                forms.multichoice("person_ids", "Assignees", people),
            ]
        return fields, people

    def _add_task(self, view):
        fields, people = self._task_fields(view.days)
        vals = forms.prompt(self, f"Add task — {view.name}", fields,
                            submit_label="Add")
        if not vals:
            return
        # First assignee becomes POIC by default (mirrors the route).
        run_write(
            self, commands.create_task(
                view.id, name=vals["name"],
                scheduled_date=vals.get("scheduled_date"),
                category_id=vals.get("category_id"),
                description=vals.get("description"),
                person_ids=vals.get("person_ids") or []),
            pii_texts=(vals.get("description"),),
            on_done=lambda: self.app.show("worklists", worklist_id=view.id))

    def _edit_task(self, task_id):
        initial = read(Q.task_get(task_id))
        if not initial:
            return
        view = read(Q.worklist_show(initial["worklist_id"]))
        fields, _ = self._task_fields(view.days if view else [], include_status=True)
        # Show who's currently assigned (editing assignees is a follow-up;
        # for now the edit form covers task fields + status/hours).
        assignees = ", ".join(initial.get("assignees") or []) or "none"
        title = f"Edit task — {assignees}"
        vals = forms.prompt(self, title, fields, initial=initial, submit_label="Save")
        if not vals:
            return
        run_write(
            self, commands.update_task(
                task_id, name=vals["name"],
                scheduled_date=vals.get("scheduled_date"),
                category_id=vals.get("category_id"),
                status=vals.get("status") or "open",
                hours=vals.get("hours"),
                description=vals.get("description"),
                completion_notes=vals.get("completion_notes")),
            pii_texts=(vals.get("description"), vals.get("completion_notes")),
            on_done=lambda: self.app.show(
                "worklists", worklist_id=initial["worklist_id"]))
