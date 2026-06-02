"""A small modal form dialog used by the write flows.

``prompt(parent, title, fields, initial=...)`` opens a centred modal,
collects values, and returns a dict keyed by field name (or ``None`` if
cancelled). Field specs are ``Field`` instances; each kind maps to a Tk
widget and a small read/normalise step.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field as dc_field
from tkinter import ttk
from typing import Any

from . import theme


@dataclass
class Field:
    name: str
    label: str
    # text | multiline | date | time | int | float | choice | combo |
    # multichoice | bool
    kind: str = "text"
    required: bool = False
    choices: list[tuple[Any, str]] = dc_field(default_factory=list)  # (value, label)
    help: str | None = None
    width: int = 32
    # Conditional visibility: (other_field_name, predicate(value) -> bool).
    # The field's row hides (and reads as None) when the predicate is False.
    visible_when: tuple[str, Any] | None = None


# Convenience constructors keep call sites readable.
def text(name, label, **kw):
    return Field(name, label, "text", **kw)


def multiline(name, label, **kw):
    return Field(name, label, "multiline", **kw)


def date(name, label, **kw):
    kw.setdefault("help", "YYYY-MM-DD")
    return Field(name, label, "date", **kw)


def time_(name, label, **kw):
    kw.setdefault("help", "HH:MM")
    return Field(name, label, "time", **kw)


def integer(name, label, **kw):
    return Field(name, label, "int", **kw)


def number(name, label, **kw):
    return Field(name, label, "float", **kw)


def choice(name, label, choices, **kw):
    return Field(name, label, "choice", choices=choices, **kw)


def combo(name, label, choices, **kw):
    """An editable dropdown: pick a listed value or type a custom one."""
    return Field(name, label, "combo", choices=choices, **kw)


def multichoice(name, label, choices, **kw):
    return Field(name, label, "multichoice", choices=choices, **kw)


def boolean(name, label, **kw):
    return Field(name, label, "bool", **kw)


class _FormDialog(tk.Toplevel):
    def __init__(self, parent, title, fields, initial, submit_label):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=theme.BG, padx=18, pady=16)
        self.transient(parent)
        self.resizable(False, False)
        self.result: dict | None = None
        self._fields = fields
        self._vars: dict[str, Any] = {}
        self._widgets: dict[str, Any] = {}
        # Per-field row widgets (label, input, help) so visible_when can
        # show/hide a whole row. Keyed by field name.
        self._rows: dict[str, list] = {}
        self._field_by_name = {f.name: f for f in fields}
        initial = initial or {}

        ttk.Label(self, text=title, style="H2.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )

        for i, f in enumerate(fields, start=1):
            lbl = ttk.Label(
                self, text=f.label + (" *" if f.required else ""), style="TLabel"
            )
            lbl.grid(row=i, column=0, sticky="nw", pady=4, padx=(0, 10))
            self._rows.setdefault(f.name, []).append((lbl, i, 0))
            self._build_field(f, i, initial.get(f.name))

        # Wire conditional visibility: when a controller field changes, the
        # dependent rows re-evaluate. Triggers fire on combobox select and
        # on entry edits.
        traced: set[str] = set()
        for f in fields:
            for ctrl_name, _pred in self._visibility_conditions(f):
                if ctrl_name in traced:
                    continue
                ctrl = self._vars.get(ctrl_name)
                if isinstance(ctrl, tk.StringVar):
                    ctrl.trace_add("write", lambda *_: self._apply_visibility())
                    traced.add(ctrl_name)
        self._apply_visibility()

        self._error = ttk.Label(self, text="", style="TLabel", foreground=theme.BAD)
        self._error.grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        btns = ttk.Frame(self)
        btns.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(
            btns, text=submit_label, style="Accent.TButton", command=self._submit
        ).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Return>", lambda e: self._submit())
        self._center(parent)
        self.grab_set()
        if fields:
            first = self._widgets[fields[0].name]
            if hasattr(first, "focus_set"):
                first.focus_set()

    def _build_field(self, f: Field, row: int, value):
        if f.kind == "multiline":
            w = tk.Text(
                self,
                width=f.width,
                height=4,
                relief="solid",
                bd=1,
                bg=theme.PANEL,
                fg=theme.TEXT,
                highlightthickness=0,
            )
            if value:
                w.insert("1.0", str(value))
            w.grid(row=row, column=1, sticky="ew", pady=4)
            self._widgets[f.name] = w
        elif f.kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            w = ttk.Checkbutton(self, variable=var)
            w.grid(row=row, column=1, sticky="w", pady=4)
            self._vars[f.name] = var
            self._widgets[f.name] = w
        elif f.kind == "choice":
            var = tk.StringVar()
            labels = [lbl for _, lbl in f.choices]
            w = ttk.Combobox(
                self,
                textvariable=var,
                values=labels,
                state="readonly",
                width=f.width - 2,
            )
            # Preselect by value or label.
            for val, lbl in f.choices:
                if value is not None and (value == val or value == lbl):
                    var.set(lbl)
                    break
            else:
                if labels and not f.required:
                    pass
            w.grid(row=row, column=1, sticky="ew", pady=4)
            self._vars[f.name] = var
            self._widgets[f.name] = w
        elif f.kind == "combo":
            # Editable dropdown: choose a listed value or type a custom one.
            var = tk.StringVar(value="" if value is None else str(value))
            labels = [lbl for _, lbl in f.choices]
            w = ttk.Combobox(
                self, textvariable=var, values=labels, state="normal", width=f.width - 2
            )
            w.grid(row=row, column=1, sticky="ew", pady=4)
            self._vars[f.name] = var
            self._widgets[f.name] = w
        elif f.kind == "multichoice":
            # A scrollable checkbox list — picks any subset of choices.
            box = tk.Frame(
                self, bg=theme.PANEL, bd=1, relief="solid", highlightthickness=0
            )
            box.grid(row=row, column=1, sticky="ew", pady=4)
            canvas = tk.Canvas(
                box,
                bg=theme.PANEL,
                highlightthickness=0,
                height=min(180, 24 * max(1, len(f.choices))),
            )
            sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
            inner = tk.Frame(canvas, bg=theme.PANEL)
            canvas.configure(yscrollcommand=sb.set)
            canvas.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            canvas.create_window((0, 0), window=inner, anchor="nw")
            inner.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
            )

            # Mouse-wheel scrolling while the pointer is over the list.
            def _wheel(e, c=canvas):
                if e.num == 5 or e.delta < 0:
                    c.yview_scroll(1, "units")
                elif e.num == 4 or e.delta > 0:
                    c.yview_scroll(-1, "units")

            def _bind_wheel(_e, c=canvas):
                c.bind_all("<MouseWheel>", _wheel)
                c.bind_all("<Button-4>", _wheel)
                c.bind_all("<Button-5>", _wheel)

            def _unbind_wheel(_e, c=canvas):
                c.unbind_all("<MouseWheel>")
                c.unbind_all("<Button-4>")
                c.unbind_all("<Button-5>")

            canvas.bind("<Enter>", _bind_wheel)
            canvas.bind("<Leave>", _unbind_wheel)
            preset = set(value or ())
            vars_for_choice: list[tuple[Any, tk.BooleanVar]] = []
            for val, lbl in f.choices:
                bv = tk.BooleanVar(value=val in preset)
                tk.Checkbutton(
                    inner,
                    text=lbl,
                    variable=bv,
                    bg=theme.PANEL,
                    fg=theme.TEXT,
                    anchor="w",
                    highlightthickness=0,
                    activebackground=theme.PANEL,
                    selectcolor=theme.PANEL,
                ).pack(fill="x", anchor="w")
                vars_for_choice.append((val, bv))
            self._vars[f.name] = vars_for_choice
            self._widgets[f.name] = box
        else:
            var = tk.StringVar(value="" if value is None else str(value))
            w = ttk.Entry(self, textvariable=var, width=f.width)
            w.grid(row=row, column=1, sticky="ew", pady=4)
            self._vars[f.name] = var
            self._widgets[f.name] = w
        # Track the input widget for visibility toggling.
        self._rows.setdefault(f.name, []).append((self._widgets[f.name], row, 1))
        if f.help:
            help_lbl = ttk.Label(self, text=f.help, style="Muted.TLabel")
            help_lbl.grid(row=row, column=2, sticky="w", padx=(8, 0))
            self._rows[f.name].append((help_lbl, row, 2))

    @staticmethod
    def _visibility_conditions(f: Field):
        """Normalise visible_when into a list of (controller, predicate).

        Accepts a single ``(name, pred)`` or a list of them (all must pass).
        """
        if f.visible_when is None:
            return []
        vw = f.visible_when
        if vw and isinstance(vw[0], str):  # single (name, pred)
            return [vw]
        return list(vw)

    def _is_visible(self, f: Field) -> bool:
        for ctrl_name, predicate in self._visibility_conditions(f):
            ctrl = self._field_by_name.get(ctrl_name)
            if ctrl is None:
                continue
            try:
                if not predicate(self._read(ctrl)):
                    return False
            except Exception:
                continue
        return True

    def _apply_visibility(self):
        for f in self._fields:
            if f.visible_when is None:
                continue
            show = self._is_visible(f)
            for widget, row, col in self._rows.get(f.name, []):
                if show:
                    widget.grid(row=row, column=col)
                else:
                    widget.grid_remove()

    def _read(self, f: Field):
        if f.kind == "multiline":
            return self._widgets[f.name].get("1.0", "end").strip() or None
        if f.kind == "bool":
            return self._vars[f.name].get()
        if f.kind == "choice":
            chosen = self._vars[f.name].get()
            for val, lbl in f.choices:
                if lbl == chosen:
                    return val
            return None
        if f.kind == "multichoice":
            return [val for val, bv in self._vars[f.name] if bv.get()]
        if f.kind == "combo":
            # Map a chosen label back to its value; otherwise keep the typed text.
            raw = self._vars[f.name].get().strip()
            for val, lbl in f.choices:
                if lbl == raw:
                    return val
            return raw or None
        raw = self._vars[f.name].get().strip()
        if f.kind == "int":
            return int(raw) if raw else None
        if f.kind == "float":
            return float(raw) if raw else None
        return raw or None

    def _submit(self):
        values: dict = {}
        for f in self._fields:
            # Hidden (conditionally-invisible) fields contribute nothing and
            # skip validation.
            if not self._is_visible(f):
                values[f.name] = None
                continue
            try:
                v = self._read(f)
            except ValueError:
                self._error.configure(text=f"{f.label}: expected a number")
                return
            if f.required and v in (None, "", False) and f.kind != "bool":
                self._error.configure(text=f"{f.label} is required")
                return
            values[f.name] = v
        self.result = values
        self.grab_release()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()

    def _center(self, parent):
        self.update_idletasks()
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        except tk.TclError:
            pass


def prompt(parent, title, fields, initial=None, submit_label="Save") -> dict | None:
    dlg = _FormDialog(parent, title, fields, initial, submit_label)
    parent.wait_window(dlg)
    return dlg.result
