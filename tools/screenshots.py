"""Take screenshots of every page in the app, desktop and mobile.

This tool is intentionally isolated: it creates a temporary SQLite database,
seeds demo data, starts a local server on a random port, and injects a local
HTTP-compatible session cookie. It never targets DATABASE_URL or the normal
dev database.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright


SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
DESKTOP_DIR = SCREENSHOTS_DIR / "desktop"
MOBILE_DIR = SCREENSHOTS_DIR / "mobile"

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
HOST = "127.0.0.1"
COOKIE_NAME = "tbdtask_session_local"

PUBLIC_PAGES = [
    ("/login", "01-login"),
]

AUTH_PAGES = [
    ("/", "02-dashboard"),
    ("/no-orgs", "03-no-orgs"),
    ("/orgs/select", "04-org-select"),
    ("/today", "05-today"),
    ("/personnel", "06-personnel"),
    ("/personnel/incoming", "07-personnel-incoming"),
    ("/personnel/departed", "08-personnel-departed"),
    ("/absences", "09-absences"),
    ("/absences/calendar", "10-absences-calendar"),
    ("/quals", "11-quals"),
    ("/quals/matrix", "12-quals-matrix"),
    ("/quals/overview", "13-quals-overview"),
    ("/worklists", "14-worklists"),
    ("/worklists/new", "15-worklist-new"),
    ("/task-templates", "16-task-templates"),
    ("/alerts", "17-alerts"),
    ("/admin", "18-admin"),
    ("/admin/members", "19-admin-members"),
    ("/admin/invites", "20-admin-invites"),
    ("/admin/roles", "21-admin-roles"),
    ("/admin/workcenters", "22-admin-workcenters"),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _tool_env(data_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("TBDTASK_SINGLE_TENANT", None)
    env["TBDTASK_DATA_DIR"] = data_dir
    env["TBDTASK_DB_FILE"] = "screenshots.db"
    env["TBDTASK_INSECURE_LOCAL_COOKIES"] = "1"
    env.setdefault("OIDC_GOOGLE_CLIENT_ID", "dummy-google-client-id.apps.googleusercontent.com")
    env.setdefault("OIDC_GOOGLE_CLIENT_SECRET", "dummy-google-secret")
    return env


def seed_data(env: dict[str, str]) -> dict[str, str] | None:
    """Create org, user, membership, role grants, and sample data."""
    result = subprocess.run(
        [sys.executable, "-c", SEED_SCRIPT],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )
    if result.returncode != 0:
        print(f"Seed failed: {result.stderr}", file=sys.stderr)
        return None
    return json.loads(result.stdout.strip())


async def take_screenshot(page, base_url: str, url: str, name: str, viewport_label: str, out_dir: Path) -> bool:
    try:
        await page.goto(f"{base_url}{url}", wait_until="networkidle", timeout=10000)
    except Exception as exc:
        print(f"  [warn] {name} ({viewport_label}): {exc}")
    body = (await page.locator("body").inner_text()).strip()
    failed = body in (
        '{"detail":"permission denied"}',
        '{"detail":"not authenticated"}',
    ) or "permission denied" in body.lower() or "not authenticated" in body.lower()
    filepath = out_dir / f"{name}-{viewport_label.lower()}.png"
    await page.screenshot(path=str(filepath), full_page=True)
    if failed:
        print(f"  [fail] {name} ({viewport_label}) captured auth error")
        return False
    print(f"  [ok] {name} ({viewport_label})")
    return True


async def main() -> None:
    for directory in (DESKTOP_DIR, MOBILE_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tbdtask-screenshots-") as data_dir:
        env = _tool_env(data_dir)
        sessions = seed_data(env)
        if not sessions:
            print("Failed to seed data.", file=sys.stderr)
            return

        port = _free_port()
        base_url = f"http://{HOST}:{port}"
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                HOST,
                "--port",
                str(port),
            ],
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        ready = False
        for _ in range(30):
            if server_proc.poll() is not None:
                stdout, stderr = server_proc.communicate()
                print(stdout, file=sys.stdout)
                print(stderr, file=sys.stderr)
                print("Server exited before becoming ready.", file=sys.stderr)
                return
            try:
                response = urllib.request.urlopen(f"{base_url}/login", timeout=2)
                if response.status == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(1)

        if not ready:
            server_proc.terminate()
            stdout, stderr = server_proc.communicate(timeout=10)
            print(stdout, file=sys.stdout)
            print(stderr, file=sys.stderr)
            print("Server failed to start.", file=sys.stderr)
            return

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                failures = []
                for viewport_label, viewport, out_dir in [
                    ("Desktop", DESKTOP_VIEWPORT, DESKTOP_DIR),
                    ("Mobile", MOBILE_VIEWPORT, MOBILE_DIR),
                ]:
                    print(f"\n=== {viewport_label} ===")
                    for url, name in PUBLIC_PAGES:
                        context = await browser.new_context(viewport=viewport)
                        page = await context.new_page()
                        if not await take_screenshot(page, base_url, url, name, viewport_label, out_dir):
                            failures.append(f"{name}-{viewport_label}")
                        await context.close()

                    for url, name in AUTH_PAGES:
                        session_key = "main"
                        if url == "/no-orgs":
                            session_key = "no_orgs"
                        elif url == "/orgs/select":
                            session_key = "org_picker"
                        context = await browser.new_context(viewport=viewport)
                        await context.add_cookies([
                            {
                                "name": COOKIE_NAME,
                                "value": sessions[session_key],
                                "domain": HOST,
                                "path": "/",
                            }
                        ])
                        page = await context.new_page()
                        if not await take_screenshot(page, base_url, url, name, viewport_label, out_dir):
                            failures.append(f"{name}-{viewport_label}")
                        await context.close()
                await browser.close()

                if failures:
                    raise RuntimeError(f"Screenshots captured auth errors: {', '.join(failures)}")
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait()

    total = (len(PUBLIC_PAGES) + len(AUTH_PAGES)) * 2
    print(f"\nDone! {total} screenshots saved to {SCREENSHOTS_DIR}")


SEED_SCRIPT = r'''
import os
import json
from datetime import date

os.environ.pop("DATABASE_URL", None)
os.environ.pop("TBDTASK_SINGLE_TENANT", None)
os.environ["TBDTASK_INSECURE_LOCAL_COOKIES"] = "1"

from app.db import init_db, SessionLocal

init_db()

from app import models as M
from app.auth import sessions as sess_mod
from app.auth.permissions import ROLE_OWNER, ROLE_ADMIN, ROLE_LPO, ROLE_MEMBER
from sqlalchemy import select

s = SessionLocal()

org = M.Organization(slug="demo-org", name="Demo Organization")
s.add(org)
s.flush()

for tmpl in (ROLE_OWNER, ROLE_ADMIN, ROLE_LPO, ROLE_MEMBER):
    role = M.Role(
        org_id=org.id,
        template_slug=tmpl.slug,
        name=tmpl.name,
        description=tmpl.description,
        builtin=True,
        workcenter_scopable=tmpl.workcenter_scopable,
    )
    s.add(role)
    s.flush()
    for permission_code in tmpl.permissions:
        s.add(M.RolePermission(role_id=role.id, permission_code=permission_code))

alice = M.UserAccount(email="alice@example.com")
bob = M.UserAccount(email="bob@example.com")
no_org_user = M.UserAccount(email="new.person@example.com")
multi_user = M.UserAccount(email="multi.user@example.com")
s.add_all([alice, bob, no_org_user, multi_user])
s.flush()

mem_alice = M.OrgMembership(org_id=org.id, user_id=alice.id, status="active")
mem_bob = M.OrgMembership(org_id=org.id, user_id=bob.id, status="active")
s.add_all([mem_alice, mem_bob])
s.flush()

org_two = M.Organization(slug="demo-org-two", name="Demo Organization Two")
s.add(org_two)
s.flush()
mem_multi_a = M.OrgMembership(org_id=org.id, user_id=multi_user.id, status="active")
mem_multi_b = M.OrgMembership(org_id=org_two.id, user_id=multi_user.id, status="active")
s.add_all([mem_multi_a, mem_multi_b])
s.flush()

owner_role = s.execute(
    select(M.Role).where(M.Role.org_id == org.id, M.Role.template_slug == "org_owner")
).scalar_one()
member_role = s.execute(
    select(M.Role).where(M.Role.org_id == org.id, M.Role.template_slug == "member")
).scalar_one()
s.add(M.MembershipRole(membership_id=mem_alice.id, role_id=owner_role.id, workcenter_id=None))
s.add(M.MembershipRole(membership_id=mem_bob.id, role_id=member_role.id, workcenter_id=None))

p1 = M.Person(first_name="John", last_name="Doe", full_display="Coordinator Doe", org_id=org.id)
p2 = M.Person(first_name="Jane", last_name="Smith", full_display="Analyst Smith", org_id=org.id)
s.add_all([p1, p2])
s.flush()
s.add(M.PersonRate(person_id=p1.id, rate="Coordinator", paygrade="L2", valid_from=date.today(), org_id=org.id))
s.add(M.PersonRosterStatus(person_id=p1.id, status="active", valid_from=date.today(), org_id=org.id))
s.add(M.PersonRate(person_id=p2.id, rate="Analyst", paygrade="L2", valid_from=date.today(), org_id=org.id))
s.add(M.PersonRosterStatus(person_id=p2.id, status="active", valid_from=date.today(), org_id=org.id))

wl = M.Worklist(week_starting=date(2026, 5, 4), name="Week of May 4", org_id=org.id)
s.add(wl)
s.flush()
cat = M.TaskCategory(name="Maintenance", display_order=0, org_id=org.id)
s.add(cat)
s.flush()
s.add(M.TaskInstance(
    worklist_id=wl.id,
    name="Weekly inspection",
    status="open",
    scheduled_date=date(2026, 5, 5),
    category_id=cat.id,
    org_id=org.id,
))
s.add(M.Qualification(name="Safety Certification", display_order=0, org_id=org.id))
s.add(M.AbsenceCode(code="Leave", display_order=0, org_id=org.id))
s.add(M.Workcenter(name="Deck", slug="deck", display_order=0, org_id=org.id))

session = sess_mod.create_session(
    s,
    user_id=alice.id,
    membership_id=mem_alice.id,
    ip="127.0.0.1",
    user_agent="screenshot-bot",
)
no_org_session = sess_mod.create_session(
    s,
    user_id=no_org_user.id,
    membership_id=None,
    ip="127.0.0.1",
    user_agent="screenshot-bot",
)
org_picker_session = sess_mod.create_session(
    s,
    user_id=multi_user.id,
    membership_id=None,
    ip="127.0.0.1",
    user_agent="screenshot-bot",
)
s.add(M.AuthEvent(
    kind="login_success",
    user_id=alice.id,
    session_id=session.id,
    provider="google",
    detail={"new_account": False},
    ip="127.0.0.1",
    user_agent="screenshot-bot",
))
s.commit()
print(json.dumps({
    "main": session.id,
    "no_orgs": no_org_session.id,
    "org_picker": org_picker_session.id,
}))
s.close()
'''


if __name__ == "__main__":
    asyncio.run(main())
