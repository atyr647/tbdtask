"""
Capture a screenshot of every UI route into ./screenshots/.

Uses headless Chrome (chrome-for-testing) at /opt/chrome-linux64/chrome.
Override with CHROME=/path/to/chrome.

Run AFTER `python -m tools.migrate_from_xlsx && python -m tools.seed_demo`,
with the dev server already running on :8765 (or pass --base-url).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

CHROME = os.environ.get("CHROME", "/opt/chrome-linux64/chrome")
DEFAULT_BASE = "http://127.0.0.1:8765"
OUT_DIR = Path(__file__).resolve().parent.parent / "screenshots"


def _slug(path: str) -> str:
    s = path.strip("/").replace("/", "_") or "home"
    s = re.sub(r"[^a-zA-Z0-9_.-]", "_", s)
    return s


def _resolve_dynamic(base_url: str, paths: list[str]) -> list[tuple[str, str]]:
    """Resolve placeholders like {worklist_id} / {task_id} / {person_id} / {qual_id} / {pq_id}
    by scraping the corresponding index pages."""
    def _first_id(html: str, pattern: str) -> str | None:
        m = re.search(pattern, html)
        return m.group(1) if m else None

    def _fetch(url: str) -> str:
        with urlopen(base_url + url, timeout=10) as r:
            return r.read().decode("utf-8", errors="ignore")

    worklists_html = _fetch("/worklists")
    worklist_id = _first_id(worklists_html, r"/worklists/(\d+)")

    quals_html = _fetch("/quals")
    qual_id = _first_id(quals_html, r"/quals/(\d+)/edit")

    personnel_html = _fetch("/personnel")
    person_id = _first_id(personnel_html, r"/personnel/(\d+)")

    person_show_html = _fetch(f"/personnel/{person_id}")
    pq_id = _first_id(person_show_html, r"/quals/(\d+)/edit")

    wl_show_html = _fetch(f"/worklists/{worklist_id}")
    task_id = _first_id(wl_show_html, r"/tasks/(\d+)/edit")

    template_id = None
    templates_html = _fetch("/task-templates")
    template_id = _first_id(templates_html, r"/task-templates/(\d+)/edit")

    absences_html = _fetch("/absences")
    absence_id = _first_id(absences_html, r"/absences/(\d+)/edit")

    alerts_html = _fetch("/alerts")
    alert_id = _first_id(alerts_html, r"/alerts/(\d+)/dismiss")

    subs = {
        "{worklist_id}": worklist_id or "1",
        "{task_id}": task_id or "1",
        "{person_id}": person_id or "1",
        "{qual_id}": qual_id or "1",
        "{pq_id}": pq_id or "1",
        "{template_id}": template_id or "1",
        "{absence_id}": absence_id or "1",
        "{alert_id}": alert_id or "1",
    }
    out: list[tuple[str, str]] = []
    for p in paths:
        resolved = p
        for k, v in subs.items():
            resolved = resolved.replace(k, v)
        out.append((p, resolved))
    return out


PAGE_PATHS = [
    # Dashboard / overview
    "/",
    "/today",
    "/alerts",
    "/alerts?show=history",
    # Worklists
    "/worklists",
    "/worklists/new",
    "/worklists/{worklist_id}",
    "/worklists/{worklist_id}/carry-over",
    "/worklists/{worklist_id}/print",
    # Tasks + templates
    "/tasks/{task_id}/edit",
    "/task-templates",
    "/task-templates/new",
    "/task-templates/{template_id}/edit",
    # Personnel
    "/personnel",
    "/personnel/new",
    "/personnel/{person_id}",
    "/personnel/{person_id}/edit",
    "/personnel/{person_id}/quals/new",
    "/personnel/{person_id}/quals/{pq_id}/edit",
    "/personnel/{person_id}/absences/new",
    # Quals
    "/quals",
    "/quals/new",
    "/quals/{qual_id}/edit",
    "/quals/overview",
    "/quals/matrix",
    # Absences
    "/absences",
    "/absences/new",
    "/absences/{absence_id}/edit",
    "/absences/calendar",
    # Admin
    "/admin/verify",
]


def screenshot(url: str, out_path: Path, *, width: int = 1280, height: int = 1600, full_page: bool = True) -> bool:
    """Capture a full-page screenshot via headless chrome.

    full_page is approximated by passing a tall window-size; chrome's
    --screenshot draws the viewport, so we make the viewport tall enough."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--virtual-time-budget=5000",
        f"--window-size={width},{height}",
        f"--screenshot={out_path}",
        url,
    ]
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return out_path.exists() and out_path.stat().st_size > 0


def print_pdf(url: str, out_path: Path) -> bool:
    """Capture a print-format PDF for the worklist print view."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--virtual-time-budget=5000",
        "--no-pdf-header-footer",
        f"--print-to-pdf={out_path}",
        url,
    ]
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return out_path.exists() and out_path.stat().st_size > 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not Path(CHROME).exists():
        print(f"chrome not found at {CHROME}", file=sys.stderr)
        return 2

    resolved = _resolve_dynamic(args.base_url, PAGE_PATHS)
    print(f"Capturing {len(resolved)} pages from {args.base_url}")
    for tmpl, path in resolved:
        url = args.base_url + path
        out = out_dir / (_slug(tmpl) + ".png")
        # Tall viewport so most pages capture full content. Print page uses
        # landscape sizing via its own CSS but the screenshot still works.
        ok = screenshot(url, out, width=1400, height=2000)
        flag = "✓" if ok else "✗"
        print(f"  {flag} {path:50s} -> {out.name}")

    # Sample worklist as PDF (browser print emulation against /worklists/{id}/print).
    wl_id = next((path for tmpl, path in resolved if tmpl == "/worklists/{worklist_id}/print"), None)
    if wl_id:
        pdf_path = out_dir / "sample_worklist.pdf"
        ok = print_pdf(args.base_url + wl_id, pdf_path)
        print(f"  {'✓' if ok else '✗'} {wl_id} -> {pdf_path.name}")

    print(f"\nDone. Screenshots in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
