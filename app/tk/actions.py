"""Glue between screen buttons and the command layer.

Centralises the three things every write needs: the PII/CUI acknowledge
gate (matching the web flow), turning a ``ValidationError`` into a tidy
message box instead of the error boundary, and refreshing the screen on
success.
"""

from __future__ import annotations

from tkinter import messagebox

from . import commands
from .context import write


def run_write(parent, cmd, *, pii_texts=(), confirm=None, on_done=None):
    """Execute a command closure with the standard guards.

    * ``pii_texts`` — free-text values to scan; if any PII/CUI is detected
      the operator must acknowledge before the write proceeds.
    * ``confirm`` — optional (title, message); shows a yes/no first.
    * ``on_done`` — called after a successful commit (typically a refresh).
    """
    if confirm is not None:
        title, message = confirm
        if not messagebox.askyesno(title, message, parent=parent):
            return None

    warnings = commands.sensitive_warnings(*pii_texts)
    if warnings:
        labels = "\n  • ".join(warnings)
        if not messagebox.askokcancel(
            "Sensitive information detected",
            "This text looks like it may contain:\n  • " + labels + "\n\nSave anyway?",
            parent=parent,
        ):
            return None

    try:
        result = write(cmd)
    except commands.ValidationError as e:
        messagebox.showerror("Couldn't save", str(e), parent=parent)
        return None

    if on_done is not None:
        on_done()
    return result
