"""Landscape-PDF renderer for a weekly worklist.

This is the native replacement for the browser print path
(``worklists/print.html`` + ``print.css`` + ``auto-print.js``). The web
app leaned on the browser engine to lay out and paginate the single-page
landscape grid; on the Pi there's no browser to lean on, so we render the
same person-row x day-column grid straight to PDF with ReportLab.

ReportLab is an optional dependency (the ``print`` extra). It's imported
lazily inside :func:`render_worklist_pdf` so the rest of ``app.tk`` runs
without it; :func:`available` lets the UI degrade gracefully and tell the
operator how to enable printing.

The layout mirrors the legacy print template:
  * landscape page, worklist title + lock/amendment line in the header
  * a Person | Team | <day columns> table; the header row repeats on every
    printed page (ReportLab ``repeatRows=1``)
  * absences shown as a coloured tag in the cell, tasks as a list with a
    "Lead" marker and shared-assignee note
  * an "Out" footer row summarising each day's absences
  * an "All hands / no specific assignee" section for unassigned tasks
"""

from __future__ import annotations

from . import dto, theme


def available() -> bool:
    """True if ReportLab is importable, so printing can proceed."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


def render_worklist_pdf(grid: dto.WeekGridDTO, out_path: str) -> str:
    """Render ``grid`` to a landscape PDF at ``out_path``; return the path.

    Raises ``RuntimeError`` if ReportLab isn't installed — callers should
    check :func:`available` first and surface the install hint.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as e:  # pragma: no cover - guarded by available()
        raise RuntimeError(
            "PDF export needs ReportLab. Install it with:\n"
            "    pip install 'tbdtask[print]'   (or: pip install reportlab)"
        ) from e

    def hx(c: str):
        return colors.HexColor(c)

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "cell", parent=styles["Normal"], fontSize=6.5, leading=8,
        alignment=TA_LEFT, textColor=hx(theme.TEXT))
    task_style = ParagraphStyle("task", parent=base, spaceAfter=1.5)
    head_style = ParagraphStyle(
        "colhead", parent=styles["Normal"], fontSize=7.5, leading=9,
        textColor=hx(theme.TEXT), fontName="Helvetica-Bold")
    name_style = ParagraphStyle(
        "name", parent=base, fontSize=7, fontName="Helvetica-Bold")
    out_style = ParagraphStyle("out", parent=base, fontSize=6, leading=7.5,
                               textColor=hx(theme.BAD))

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # --- cell renderer ---------------------------------------------------
    def cell_flowables(cell: dto.GridCellDTO):
        flow = []
        if cell.absence_code:
            bits = [f"<b>{esc(cell.absence_code)}</b>"]
            if cell.absence_span:
                bits.append(esc(cell.absence_span))
            if cell.absence_reason:
                bits.append(f"<i>{esc(cell.absence_reason)}</i>")
            flow.append(Paragraph(" ".join(bits), out_style))
        for t in cell.tasks:
            parts = []
            if t.is_poic:
                parts.append('<b>Lead</b>')
            parts.append(esc(t.name))
            if t.category:
                parts.append(f"<i>({esc(t.category)})</i>")
            line = " ".join(parts)
            if t.other_assignees:
                line += (f"<br/><font size=5 color='{theme.MUTED}'>w/ "
                         f"{esc(', '.join(t.other_assignees))}</font>")
            flow.append(Paragraph(line, task_style))
        return flow or [Paragraph("", base)]

    # --- table data ------------------------------------------------------
    header_row = [Paragraph("<b>Person</b>", head_style),
                  Paragraph("<b>Team</b>", head_style)]
    for h in grid.headers:
        txt = (f"<b>{esc(h.weekday)}</b><br/>{esc(h.date_label)}<br/>"
               f"<font size=6>{h.percent_present:g}% · "
               f"{h.present_count}/{h.present_count + h.out_count}"
               + (f" ({h.out_count} out)" if h.out_count else "") + "</font>")
        header_row.append(Paragraph(txt, head_style))

    data = [header_row]
    absent_cells: list[tuple[int, int]] = []  # (row_idx, col_idx) for shading
    for r_i, row in enumerate(grid.rows, start=1):
        line = [Paragraph(esc(row.name), name_style),
                Paragraph(str(row.duty_section or ""), base)]
        for c_i, cell in enumerate(row.cells, start=2):
            if cell.absence_code:
                absent_cells.append((r_i, c_i))
            line.append(cell_flowables(cell))
        data.append(line)

    # Out-summary footer row (repeats per page via being inside the table).
    foot = [Paragraph("<b>Out</b>", name_style), Paragraph("", base)]
    foot_row_idx = len(data)
    for h in grid.headers:
        if h.out_summary:
            txt = "<br/>".join(esc(s) for s in h.out_summary)
            foot.append(Paragraph(txt, out_style))
        else:
            foot.append(Paragraph(f"<font color='{theme.MUTED}'>—</font>", base))
    data.append(foot)

    # --- column widths ---------------------------------------------------
    page_w, page_h = landscape(letter)
    margin = 0.4 * inch
    avail_w = page_w - 2 * margin
    name_w = 1.25 * inch
    team_w = 0.45 * inch
    day_w = (avail_w - name_w - team_w) / max(1, len(grid.headers))
    col_widths = [name_w, team_w] + [day_w] * len(grid.headers)

    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, hx(theme.BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), hx(theme.PANEL3)),
        ("BACKGROUND", (0, foot_row_idx), (-1, foot_row_idx), hx(theme.PANEL2)),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    # Zebra striping on body rows.
    for r_i in range(1, foot_row_idx):
        if r_i % 2 == 0:
            style.append(("BACKGROUND", (0, r_i), (-1, r_i), hx(theme.PANEL2)))
    # Shade absence cells.
    for r_i, c_i in absent_cells:
        style.append(("BACKGROUND", (c_i, r_i), (c_i, r_i), hx(theme.BAD_SOFT)))
    table.setStyle(TableStyle(style))

    # --- document --------------------------------------------------------
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=15,
                                 textColor=hx(theme.TEXT), spaceAfter=2)
    meta_style = ParagraphStyle("meta", parent=styles["Normal"], fontSize=8,
                                textColor=hx(theme.MUTED))
    section_style = ParagraphStyle("section", parent=styles["Heading2"],
                                   fontSize=10, textColor=hx(theme.TEXT))

    meta_bits = [f"Week of {grid.week_starting.isoformat()}"]
    if grid.locked_label:
        meta_bits.append(grid.locked_label)
    if grid.amendment_label:
        meta_bits.append(grid.amendment_label)

    story = [
        Paragraph(esc(grid.worklist_name), title_style),
        Paragraph(" · ".join(esc(b) for b in meta_bits), meta_style),
        Spacer(1, 6),
        table,
    ]

    if grid.unassigned:
        story += [Spacer(1, 10),
                  Paragraph("All hands / no specific assignee", section_style)]
        ua_data = []
        for day in grid.unassigned:
            lines = []
            for t in day.tasks:
                p = [esc(t.name)]
                if t.external_poic:
                    p.append(f"<i>Lead: {esc(t.external_poic)}</i>")
                if t.category:
                    p.append(f"<i>({esc(t.category)})</i>")
                lines.append(" ".join(p))
            ua_data.append([Paragraph(f"<b>{esc(day.day_label)}</b>", base),
                            Paragraph("<br/>".join(lines), base)])
        ua_table = Table(ua_data, colWidths=[1.3 * inch, avail_w - 1.3 * inch])
        ua_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, hx(theme.BORDER)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(ua_table)

    doc = SimpleDocTemplate(
        out_path, pagesize=landscape(letter),
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
        title=grid.worklist_name)
    doc.build(story)
    return out_path
