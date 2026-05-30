"""Tests for the Tk front-end's worklist PDF export.

Covers the print-grid query (DTO shape) and the ReportLab renderer
(produces a valid PDF). The renderer test skips cleanly when ReportLab
isn't installed, since it's an optional dependency.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.tenancy import tenant_context
from app.tk import dto, pdf
from app.tk import commands as C
from app.tk import queries as Q

from tests.conftest import (
    make_absence,
    make_absence_codes,
    make_person,
    make_worklist,
)


def run(session, fn):
    with tenant_context(1):
        return fn(session)


@pytest.fixture
def week(session):
    codes = make_absence_codes(session)
    p1 = make_person(session, last_name="Reyes", rate="GS13", display_order=0)
    p2 = make_person(session, last_name="Holland", rate="GS12", display_order=1)
    monday = date.today() - timedelta(days=date.today().weekday())
    wl = make_worklist(session, monday)
    session.commit()
    # A task assigned to p1, and an absence for p2 on Monday.
    run(session, C.create_task(
        wl.id, name="Sweep the deck", scheduled_date=monday.isoformat(),
        person_ids=[p1.id]))
    make_absence(session, person_id=p2.id, code_id=codes["Leave"].id,
                 start_date=monday, end_date=monday, reason="family")
    session.commit()
    return wl.id


def test_week_grid_dto_shape(session, week):
    grid = run(session, Q.week_grid(week))
    assert isinstance(grid, dto.WeekGridDTO)
    assert len(grid.headers) == 5  # Mon–Fri default
    # The assigned person shows up as a row with a task.
    reyes = next((r for r in grid.rows if "Reyes" in r.name), None)
    assert reyes is not None
    assert any(c.tasks for c in reyes.cells)
    # The absent person shows up with an absence code in Monday's cell.
    holland = next((r for r in grid.rows if "Holland" in r.name), None)
    assert holland is not None
    assert holland.cells[0].absence_code == "Leave"
    # Monday's header carries an out summary line for Holland.
    assert any("Holland" in s for s in grid.headers[0].out_summary)


def test_week_grid_missing_returns_none(session):
    assert run(session, Q.week_grid(999_999)) is None


@pytest.mark.skipif(not pdf.available(), reason="ReportLab not installed")
def test_render_worklist_pdf(session, week, tmp_path):
    grid = run(session, Q.week_grid(week))
    out = tmp_path / "wl.pdf"
    path = pdf.render_worklist_pdf(grid, str(out))
    data = out.read_bytes()
    assert path == str(out)
    assert data[:5] == b"%PDF-"
    assert b"%%EOF" in data[-2048:]
    assert out.stat().st_size > 1000


@pytest.mark.skipif(not pdf.available(), reason="ReportLab not installed")
def test_font_registration_returns_a_family():
    # Resolves to a real TTF family where one is installed (CI images carry
    # Liberation/DejaVu); always returns a usable font name and is cached.
    name = pdf._register_fonts()
    assert isinstance(name, str) and name
    assert pdf._register_fonts() == name  # idempotent


def test_date_field_accepts_custom_help():
    """Regression: forms.date/time_ injected a default help= that collided
    when a caller also passed help=, crashing the template form."""
    from app.tk import forms
    f = forms.date("d", "D", help="custom")
    assert f.kind == "date" and f.help == "custom"
    assert forms.date("d2", "D2").help == "YYYY-MM-DD"
    assert forms.time_("t", "T", help="x").help == "x"


# -- forms: conditional visibility + editable combo -------------------------


def test_form_visible_when_and_combo(tmp_path):
    """visible_when hides a field (reads None) based on another field's value,
    and combo maps a chosen label to its value while allowing custom text.
    Driven headlessly under a Tk root."""
    import os
    os.environ.setdefault("TBDTASK_SINGLE_TENANT", "1")
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        import pytest
        pytest.skip("no display")
    root.withdraw()
    from app.tk import forms

    fields = [
        forms.choice("kind", "Kind", [("e", "Enlisted"), ("o", "Officer")]),
        forms.choice("rating", "Rating", [("BM", "BM"), ("IT", "IT")],
                     visible_when=("kind", lambda v: v == "e")),
        forms.combo("pos", "Pos", [("LPO", "LPO"), ("LCPO", "LCPO")]),
    ]
    dlg = forms._FormDialog(root, "t", fields, {"kind": "o", "pos": "LPO"}, "Save")
    # Officer selected -> rating row hidden -> reads as None on submit.
    assert dlg._is_visible(dlg._field_by_name["rating"]) is False
    # combo preset label maps back to its value.
    assert dlg._read(dlg._field_by_name["pos"]) == "LPO"
    # custom typed combo text passes through.
    dlg._vars["pos"].set("Custom Billet")
    assert dlg._read(dlg._field_by_name["pos"]) == "Custom Billet"
    dlg.destroy()
    root.destroy()


def test_form_multi_condition_visible_when():
    """A field with a LIST of (controller, predicate) conditions shows only
    when ALL pass (used for the undesignated-junior Community field)."""
    import os
    os.environ.setdefault("TBDTASK_SINGLE_TENANT", "1")
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError:
        import pytest
        pytest.skip("no display")
    root.withdraw()
    from app.tk import forms

    fields = [
        forms.choice("paygrade", "PG", [("E-1", "E-1"), ("E-5", "E-5")]),
        forms.choice("rating", "Rating", [("", "(none)"), ("BM", "BM")]),
        forms.choice("community", "Community", [("seaman", "Seaman"),
                                                ("fireman", "Fireman")],
                     visible_when=[("paygrade", lambda v: v in ("E-1", "E-2", "E-3")),
                                   ("rating", lambda v: not v)]),
    ]
    dlg = forms._FormDialog(root, "t", fields, {}, "Save")
    comm = dlg._field_by_name["community"]
    # E-1 + no rating -> visible
    dlg._vars["paygrade"].set("E-1")
    dlg._vars["rating"].set("(none)")
    assert dlg._is_visible(comm) is True
    # E-1 + a rating -> hidden (designated striker, community is implied)
    dlg._vars["rating"].set("BM")
    assert dlg._is_visible(comm) is False
    # E-5 + no rating -> hidden (community only applies to E1-E3)
    dlg._vars["paygrade"].set("E-5")
    dlg._vars["rating"].set("(none)")
    assert dlg._is_visible(comm) is False
    dlg.destroy()
    root.destroy()
