"""Weekly worklists — list of weeks + the day-by-day week view."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import commands, forms, pdf, theme
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
        ttk.Button(
            bar,
            text="+ New worklist",
            style="Accent.TButton",
            command=lambda: self._new_worklist(data.suggested_monday),
        ).pack(side="right")
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
        ttk.Label(parent, text=title, style="H2.TLabel").pack(anchor="w", pady=(10, 4))
        for w in rows:
            card = Card(parent)
            card.pack(fill="x", pady=3)
            row = ttk.Frame(card.body, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Label(
                row, text=w.name, style="Card.TLabel", font=theme.Fonts().bold
            ).pack(side="left")
            if w.locked:
                badge(row, "locked", status="qualified").pack(side="left", padx=8)
            if w.version > 1:
                badge(row, f"v{w.version}", status="in_progress").pack(side="left")
            if w.amendment_count:
                ttk.Label(
                    row,
                    text=f"{w.amendment_count} amendment(s)",
                    style="CardMuted.TLabel",
                ).pack(side="left", padx=8)
            ttk.Label(
                row,
                text=f"week of {w.week_starting.strftime('%d %b %Y')}",
                style="CardMuted.TLabel",
            ).pack(side="right")
            ttk.Button(
                row,
                text="Open",
                command=lambda wid=w.id: self.app.show("worklists", worklist_id=wid),
            ).pack(side="right", padx=8)

    def _show_week(self, worklist_id: int):
        view = read(Q.worklist_show(worklist_id))
        if view is None:
            self.header("Worklist not found")
            ttk.Button(
                self, text="← Back", command=lambda: self.app.show("worklists")
            ).pack(anchor="w")
            return

        bar = self.header(
            view.name, f"week of {view.week_starting.strftime('%d %b %Y')}"
        )
        ttk.Button(bar, text="← Back", command=lambda: self.app.show("worklists")).pack(
            side="right"
        )
        ttk.Button(
            bar,
            text="Print PDF",
            command=lambda: self._print_pdf(worklist_id, view.name),
        ).pack(side="right", padx=4)
        if view.locked:
            badge(bar, f"locked by {view.locked_by or '—'}", status="qualified").pack(
                side="right", padx=8
            )
            ttk.Button(
                bar,
                text="Amend",
                style="Accent.TButton",
                command=lambda: self._amend(worklist_id),
            ).pack(side="right", padx=4)
        else:
            ttk.Button(
                bar,
                text="+ Add task",
                style="Accent.TButton",
                command=lambda: self._add_task(view),
            ).pack(side="right", padx=4)
            menu = ttk.Menubutton(bar, text="More ▾")
            m = tk.Menu(menu, tearoff=False)
            m.add_command(
                label="Edit name / notes",
                command=lambda: self._edit_worklist(worklist_id, view),
            )
            m.add_command(
                label="Regenerate recurring",
                command=lambda: self._regenerate(worklist_id),
            )
            if view.pending_count:
                m.add_command(
                    label=f"Carry-over review ({view.pending_count})",
                    command=lambda: self._carry_over(worklist_id),
                )
            m.add_separator()
            m.add_command(
                label="Archive worklist", command=lambda: self._archive(worklist_id)
            )
            menu["menu"] = m
            menu.pack(side="right", padx=4)
        self._locked = view.locked
        if view.pending_count:
            badge(
                bar, f"{view.pending_count} carry-over pending", status="in_progress"
            ).pack(side="right", padx=8)
        ttk.Label(
            bar, text=f"{view.week_total_tasks} tasks", style="Muted.TLabel"
        ).pack(side="right", padx=8, pady=(10, 0))

        scroller = VScroll(self)
        scroller.pack(fill="both", expand=True)
        for day in view.days:
            if not (day.persons or day.unassigned or day.out_today):
                continue
            title = (
                f"{day.weekday} · {day.on_date.strftime('%d %b')}  "
                f"({day.percent_present:g}% present)"
            )
            card = Card(scroller.body, title=title)
            card.pack(fill="x", pady=4)
            if day.out_today:
                out = ttk.Frame(card.body, style="Card.TFrame")
                out.pack(fill="x", pady=(0, 6))
                ttk.Label(out, text="Out:", style="CardMuted.TLabel").pack(side="left")
                for r in day.out_today:
                    badge(
                        out,
                        f"{r.code or 'OUT'} {r.name}",
                        status="dinq" if not r.partial else "in_progress",
                    ).pack(side="left", padx=3)
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
            label = (
                f"{person.rate} {person.name}"
                if not label.startswith(person.rate)
                else label
            )
        ttk.Label(head, text=label, style="Card.TLabel", font=theme.Fonts().bold).pack(
            side="left"
        )
        if person.duty_section is not None:
            ttk.Label(
                head, text=f"  DS{person.duty_section}", style="CardMuted.TLabel"
            ).pack(side="left")
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
                ttk.Label(row, text=" · ".join(meta), style="CardMuted.TLabel").pack(
                    side="right"
                )

    def _bind_edit(self, widget, task_id):
        """On an unlocked week, click a task to get an action menu
        (edit / manage assignees / remove)."""
        if getattr(self, "_locked", False):
            return
        widget.configure(cursor="hand2")
        widget.bind("<Button-1>", lambda e: self._task_menu(e, task_id))

    def _task_menu(self, event, task_id):
        m = tk.Menu(self, tearoff=False)
        m.add_command(label="Edit task", command=lambda: self._edit_task(task_id))
        m.add_command(
            label="Manage assignees", command=lambda: self._manage_assignees(task_id)
        )
        m.add_separator()
        m.add_command(label="Remove task", command=lambda: self._remove_task(task_id))
        m.tk_popup(event.x_root, event.y_root)

    # -- Write actions ----------------------------------------------------
    def _new_worklist(self, suggested_monday):
        vals = forms.prompt(
            self,
            "New worklist",
            [
                forms.date("week_starting", "Week starting (Monday)", required=True),
            ],
            initial={"week_starting": suggested_monday},
            submit_label="Create",
        )
        if not vals:
            return
        wid = run_write(self, commands.create_worklist(vals["week_starting"]))
        if wid:
            self.app.show("worklists", worklist_id=wid)

    def _regenerate(self, worklist_id):
        run_write(
            self,
            commands.generate_worklist_tasks(worklist_id),
            confirm=(
                "Regenerate recurring tasks",
                "Re-run recurring templates for this week? Existing "
                "tasks are kept; only missing recurring ones are added.",
            ),
            on_done=lambda: self.app.show("worklists", worklist_id=worklist_id),
        )

    def _task_fields(self, days, include_status=False):
        people = read(commands.active_people_choices())
        cats = read(commands.task_categories_choices())
        day_choices = [
            (d.on_date.isoformat(), f"{d.weekday} · {d.on_date.strftime('%d %b')}")
            for d in days
        ]
        fields = [
            forms.text("name", "Task name", required=True),
            forms.choice("scheduled_date", "Day", day_choices),
            forms.choice("category_id", "Category", cats),
            forms.multiline("description", "Description"),
        ]
        if include_status:
            fields += [
                forms.choice(
                    "status",
                    "Status",
                    [(s, s.replace("_", " ")) for s in commands.TASK_STATUSES],
                ),
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
        vals = forms.prompt(self, f"Add task — {view.name}", fields, submit_label="Add")
        if not vals:
            return
        # First assignee becomes POIC by default (mirrors the route).
        run_write(
            self,
            commands.create_task(
                view.id,
                name=vals["name"],
                scheduled_date=vals.get("scheduled_date"),
                category_id=vals.get("category_id"),
                description=vals.get("description"),
                person_ids=vals.get("person_ids") or [],
            ),
            pii_texts=(vals.get("description"),),
            on_done=lambda: self.app.show("worklists", worklist_id=view.id),
        )

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
            self,
            commands.update_task(
                task_id,
                name=vals["name"],
                scheduled_date=vals.get("scheduled_date"),
                category_id=vals.get("category_id"),
                status=vals.get("status") or "open",
                hours=vals.get("hours"),
                description=vals.get("description"),
                completion_notes=vals.get("completion_notes"),
            ),
            pii_texts=(vals.get("description"), vals.get("completion_notes")),
            on_done=lambda: self.app.show(
                "worklists", worklist_id=initial["worklist_id"]
            ),
        )

    def _print_pdf(self, worklist_id, name):
        if not pdf.available():
            messagebox.showinfo(
                "Printing needs ReportLab",
                "PDF export requires the ReportLab library, which isn't "
                "installed.\n\nInstall it with:\n"
                "    pip install 'tbdtask[print]'\n"
                "(or: pip install reportlab)",
                parent=self,
            )
            return
        default = "".join(c if c.isalnum() else "_" for c in name) + ".pdf"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save worklist PDF",
            defaultextension=".pdf",
            initialfile=default,
            filetypes=[("PDF", "*.pdf")],
        )
        if not path:
            return
        grid = read(Q.week_grid(worklist_id))
        if grid is None:
            messagebox.showerror("Print failed", "Worklist not found.", parent=self)
            return
        try:
            pdf.render_worklist_pdf(grid, path)
        except Exception as e:  # surface render errors instead of the boundary
            messagebox.showerror("Print failed", str(e), parent=self)
            return
        if messagebox.askyesno(
            "PDF saved", f"Saved to:\n{path}\n\nOpen it now?", parent=self
        ):
            self._open_file(path)

    @staticmethod
    def _open_file(path):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # -- worklist-level actions ------------------------------------------
    def _edit_worklist(self, worklist_id, view):
        vals = forms.prompt(
            self,
            "Edit worklist",
            [
                forms.text("name", "Name", required=True),
                forms.multiline("notes", "Notes"),
            ],
            initial={"name": view.name},
            submit_label="Save",
        )
        if not vals:
            return
        run_write(
            self,
            commands.update_worklist(
                worklist_id, name=vals["name"], notes=vals.get("notes")
            ),
            pii_texts=(vals.get("notes"),),
            on_done=lambda: self.app.show("worklists", worklist_id=worklist_id),
        )

    def _amend(self, worklist_id):
        vals = forms.prompt(
            self,
            "Amend locked worklist",
            [
                forms.text("amendment_reason", "Amendment reason", required=True),
                forms.text("operator_name", "Operator"),
            ],
            submit_label="Create amendment",
        )
        if not vals:
            return
        new_id = run_write(
            self,
            commands.amend_worklist(
                worklist_id,
                amendment_reason=vals["amendment_reason"],
                operator_name=vals.get("operator_name"),
            ),
            pii_texts=(vals.get("amendment_reason"),),
        )
        if new_id:
            self.app.show("worklists", worklist_id=new_id)

    def _archive(self, worklist_id):
        vals = forms.prompt(
            self,
            "Archive worklist",
            [forms.text("reason", "Reason")],
            submit_label="Archive",
        )
        if vals is None:
            return
        run_write(
            self,
            commands.archive_worklist(worklist_id, vals.get("reason") or ""),
            confirm=(
                "Archive worklist",
                "Hide this worklist from the active lists? Its data is kept.",
            ),
            pii_texts=(vals.get("reason"),),
            on_done=lambda: self.app.show("worklists"),
        )

    # -- task assignee management ----------------------------------------
    def _manage_assignees(self, task_id):
        data = read(Q.task_assignments(task_id))
        if data is None:
            return
        if data["locked"]:
            messagebox.showinfo(
                "Locked", "Amend the worklist before editing tasks.", parent=self
            )
            return
        dlg = tk.Toplevel(self)
        dlg.title(f"Assignees — {data['task_name']}")
        dlg.configure(bg=theme.BG, padx=16, pady=14)
        dlg.transient(self)
        ttk.Label(dlg, text=data["task_name"], style="H2.TLabel").pack(anchor="w")

        listf = ttk.Frame(dlg)
        listf.pack(fill="both", expand=True, pady=8)
        if not data["assignments"]:
            empty_state(listf, "No assignees yet.").pack(anchor="w")
        for a in data["assignments"]:
            row = ttk.Frame(listf)
            row.pack(fill="x", pady=2)
            label = a["name"] + ("  (POIC)" if a["is_poic"] else "")
            ttk.Label(row, text=label, style="TLabel").pack(side="left")
            ttk.Button(
                row,
                text="Remove",
                command=lambda aid=a["id"]: self._do_assignment(
                    dlg, task_id, commands.remove_assignment(task_id, aid)
                ),
            ).pack(side="right", padx=2)
            if not a["is_poic"]:
                ttk.Button(
                    row,
                    text="Make lead",
                    command=lambda aid=a["id"]: self._do_assignment(
                        dlg, task_id, commands.set_assignment_poic(task_id, aid)
                    ),
                ).pack(side="right", padx=2)

        ttk.Button(
            dlg,
            text="+ Add assignee",
            style="Accent.TButton",
            command=lambda: self._add_assignee(dlg, task_id),
        ).pack(anchor="w", pady=(4, 0))
        ttk.Button(dlg, text="Done", command=dlg.destroy).pack(anchor="e")
        dlg.grab_set()

    def _do_assignment(self, dlg, task_id, cmd):
        run_write(self, cmd)
        dlg.destroy()
        self._manage_assignees(task_id)

    def _add_assignee(self, parent_dlg, task_id):
        people = read(commands.active_people_choices())
        vals = forms.prompt(
            parent_dlg,
            "Add assignee",
            [
                forms.choice("person_id", "Person", people),
                forms.text("external_poic_name", "…or off-roster lead name"),
                forms.boolean("is_poic", "Make lead (POIC)"),
            ],
            submit_label="Add",
        )
        if not vals:
            return
        if not vals.get("person_id") and not vals.get("external_poic_name"):
            return
        run_write(
            self,
            commands.add_assignment(
                task_id,
                person_id=vals.get("person_id"),
                external_poic_name=vals.get("external_poic_name"),
                is_poic=vals.get("is_poic"),
            ),
            pii_texts=(vals.get("external_poic_name"),),
        )
        parent_dlg.destroy()
        self._manage_assignees(task_id)

    def _remove_task(self, task_id):
        vals = forms.prompt(
            self, "Remove task", [forms.text("reason", "Reason")], submit_label="Remove"
        )
        if vals is None:
            return
        wid = read(Q.task_get(task_id))
        wl_id = wid["worklist_id"] if wid else None
        run_write(
            self,
            commands.archive_task(task_id, vals.get("reason") or ""),
            pii_texts=(vals.get("reason"),),
            on_done=lambda: (
                self.app.show("worklists", worklist_id=wl_id)
                if wl_id
                else self.app.show("worklists")
            ),
        )

    # -- carry-over review ------------------------------------------------
    def _carry_over(self, worklist_id):
        data = read(Q.carry_over_candidates(worklist_id))
        if data is None or data["locked"] or not data["candidates"]:
            messagebox.showinfo(
                "Carry-over", "No pending tasks to carry over.", parent=self
            )
            return
        dlg = tk.Toplevel(self)
        dlg.title("Carry-over review")
        dlg.configure(bg=theme.BG, padx=16, pady=14)
        dlg.transient(self)
        ttk.Label(dlg, text="Pending tasks from earlier weeks", style="H2.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        scroller = VScroll(dlg)
        scroller.pack(fill="both", expand=True)
        choices = [
            ("carry", "Carry to this week"),
            ("complete", "Mark complete"),
            ("discard", "Discard"),
            ("leave", "Leave for later"),
        ]
        actions: dict[int, tk.StringVar] = {}
        for c in data["candidates"]:
            card = Card(scroller.body)
            card.pack(fill="x", pady=3)
            ttk.Label(
                card.body, text=c["name"], style="Card.TLabel", font=theme.Fonts().bold
            ).pack(anchor="w")
            meta = []
            if c["assignees"]:
                meta.append("assignees: " + ", ".join(c["assignees"]))
            if c["source_week"]:
                meta.append(f"from week of {c['source_week']}")
            if meta:
                ttk.Label(
                    card.body, text=" · ".join(meta), style="CardMuted.TLabel"
                ).pack(anchor="w")
            var = tk.StringVar(
                value={"carry": "carry", "discard": "discard", "manual": "leave"}.get(
                    c["suggested"], "carry"
                )
            )
            actions[c["instance_id"]] = var
            rowf = ttk.Frame(card.body, style="Card.TFrame")
            rowf.pack(anchor="w", pady=(4, 0))
            for val, label in choices:
                ttk.Radiobutton(rowf, text=label, value=val, variable=var).pack(
                    side="left", padx=(0, 8)
                )

        def apply_all():
            decisions = {iid: {"action": var.get()} for iid, var in actions.items()}
            run_write(self, commands.apply_carry_overs(worklist_id, decisions))
            dlg.destroy()
            self.app.show("worklists", worklist_id=worklist_id)

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="Apply", style="Accent.TButton", command=apply_all).pack(
            side="right", padx=6
        )
        dlg.grab_set()
