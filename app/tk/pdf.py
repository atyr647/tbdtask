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

import glob
import os

from . import dto, theme


def available() -> bool:
    """True if ReportLab is importable, so printing can proceed."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


# Candidate TrueType families, best first. Each entry maps the four faces
# we use (regular/bold/italic/bold-italic) to filenames; the first family
# whose regular + bold files both exist wins. Liberation Sans is metric-
# compatible with Arial and ships on Raspberry Pi OS (fonts-liberation);
# DejaVu Sans is the near-universal fallback. If none are found we fall
# back to ReportLab's built-in Helvetica (the old, "hideous" look).
_FONT_CANDIDATES = [
    # (family_key, dir_glob, regular, bold, italic, bold_italic)
    (
        "LiberationSans",
        "/usr/share/fonts/**/LiberationSans-Regular.ttf",
        "LiberationSans-Regular.ttf",
        "LiberationSans-Bold.ttf",
        "LiberationSans-Italic.ttf",
        "LiberationSans-BoldItalic.ttf",
    ),
    (
        "DejaVuSans",
        "/usr/share/fonts/**/DejaVuSans.ttf",
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        # DejaVu has no true italic; reuse the upright faces so <i> still
        # renders (just non-slanted) rather than breaking the family map.
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ),
]

# Resolved once per process: ("FamilyName", registered: bool).
_resolved_family: tuple[str, bool] | None = None


def _register_fonts() -> str:
    """Register the best available TTF family and return its name.

    Returns ``"Helvetica"`` (ReportLab built-in) if no TrueType family is
    found. Idempotent and cached.
    """
    global _resolved_family
    if _resolved_family is not None:
        return _resolved_family[0]

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for family, probe, reg, bold, ital, bold_ital in _FONT_CANDIDATES:
        matches = glob.glob(probe, recursive=True)
        if not matches:
            continue
        font_dir = os.path.dirname(matches[0])
        faces = {
            "": os.path.join(font_dir, reg),
            "-Bold": os.path.join(font_dir, bold),
            "-Italic": os.path.join(font_dir, ital),
            "-BoldItalic": os.path.join(font_dir, bold_ital),
        }
        if not (os.path.exists(faces[""]) and os.path.exists(faces["-Bold"])):
            continue
        try:
            for suffix, path in faces.items():
                if os.path.exists(path):
                    pdfmetrics.registerFont(TTFont(family + suffix, path))
            # Map the family so <b>/<i>/<b><i> pick the right registered face.
            pdfmetrics.registerFontFamily(
                family,
                normal=family,
                bold=family + "-Bold",
                italic=family + "-Italic"
                if os.path.exists(faces["-Italic"])
                else family,
                boldItalic=family + "-BoldItalic"
                if os.path.exists(faces["-BoldItalic"])
                else family + "-Bold",
            )
        except Exception:
            continue
        _resolved_family = (family, True)
        return family

    _resolved_family = ("Helvetica", False)
    return "Helvetica"


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

    font = _register_fonts()
    font_bold = font + "-Bold" if font != "Helvetica" else "Helvetica-Bold"

    def hx(c: str):
        return colors.HexColor(c)

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "cell",
        parent=styles["Normal"],
        fontName=font,
        fontSize=7,
        leading=8.5,
        alignment=TA_LEFT,
        textColor=hx(theme.TEXT),
    )
    task_style = ParagraphStyle("task", parent=base, spaceAfter=2)
    head_style = ParagraphStyle(
        "colhead",
        parent=styles["Normal"],
        fontName=font_bold,
        fontSize=8,
        leading=10,
        textColor=hx(theme.TEXT),
    )
    name_style = ParagraphStyle("name", parent=base, fontName=font_bold, fontSize=7.5)
    out_style = ParagraphStyle(
        "out",
        parent=base,
        fontName=font,
        fontSize=6.5,
        leading=8,
        textColor=hx(theme.BAD),
    )

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
            line = esc(t.name)
            if t.other_assignees:
                line += (
                    f"<br/><font size=5 color='{theme.MUTED}'>w/ "
                    f"{esc(', '.join(t.other_assignees))}</font>"
                )
            flow.append(Paragraph(line, task_style))
        return flow or [Paragraph("", base)]

    # --- table data ------------------------------------------------------
    header_row = [
        Paragraph("<b>Person</b>", head_style),
        Paragraph("<b>Team</b>", head_style),
    ]
    for h in grid.headers:
        txt = (
            f"<b>{esc(h.weekday)}</b><br/>{esc(h.date_label)}<br/>"
            f"<font size=6>{h.percent_present:g}% · "
            f"{h.present_count}/{h.present_count + h.out_count}"
            + (f" ({h.out_count} out)" if h.out_count else "")
            + "</font>"
        )
        header_row.append(Paragraph(txt, head_style))

    data = [header_row]
    absent_cells: list[tuple[int, int]] = []  # (row_idx, col_idx) for shading
    for r_i, row in enumerate(grid.rows, start=1):
        line = [
            Paragraph(esc(row.name), name_style),
            Paragraph(str(row.duty_section or ""), base),
        ]
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
    title_style = ParagraphStyle(
        "title",
        parent=styles["Title"],
        fontName=font_bold,
        fontSize=16,
        textColor=hx(theme.TEXT),
        spaceAfter=2,
    )
    meta_style = ParagraphStyle(
        "meta",
        parent=styles["Normal"],
        fontName=font,
        fontSize=8.5,
        textColor=hx(theme.MUTED),
    )
    section_style = ParagraphStyle(
        "section",
        parent=styles["Heading2"],
        fontName=font_bold,
        fontSize=11,
        textColor=hx(theme.TEXT),
    )

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
        story += [
            Spacer(1, 10),
            Paragraph("All hands / no specific assignee", section_style),
        ]
        ua_data = []
        for day in grid.unassigned:
            lines = []
            for t in day.tasks:
                lines.append(esc(t.name))
            ua_data.append(
                [
                    Paragraph(f"<b>{esc(day.day_label)}</b>", base),
                    Paragraph("<br/>".join(lines), base),
                ]
            )
        ua_table = Table(ua_data, colWidths=[1.3 * inch, avail_w - 1.3 * inch])
        ua_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, hx(theme.BORDER)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(ua_table)

    doc = SimpleDocTemplate(
        out_path,
        pagesize=landscape(letter),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=grid.worklist_name,
    )
    doc.build(story)
    return out_path
