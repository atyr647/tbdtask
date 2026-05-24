from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# The AppImage / local desktop launcher is a single-user offline tool:
# no login, no remote access, no multi-tenant. Set the flag before any
# auth-aware module reads it. ``serve()`` is the only entry that goes
# through here; ``uvicorn app.main:app`` for hosted deployments leaves
# the env untouched and runs the full auth pipeline.
if __name__ == "__main__" or os.environ.get("TBDTASK_LAUNCHER") == "1":
    os.environ.setdefault("TBDTASK_SINGLE_TENANT", "1")

from .db import init_db  # noqa: E402
from .middleware import (
    AuthRateLimitMiddleware,
    CSRFMiddleware,
    SecureHeadersMiddleware,
    SessionMiddleware,
    TrustedProxyMiddleware,
)
from .routes import (
    absences,
    admin as admin_routes,
    alerts as alerts_routes,
    auth as auth_routes,
    home,
    onboarding as onboarding_routes,
    personnel,
    quals,
    tasks,
    templates as templates_routes,
    today,
    worklists,
)


APP_ROOT = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="Worklist Tracker", version="0.1.0")
    app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")

    # Health check — must register before middleware so it bypasses session/CSRF.
    # The middleware allowlists /healthz, but the route still needs a handler.
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # Middleware order matters (Starlette runs them outside-in for the
    # request, inside-out for the response). Read top-to-bottom as the
    # response journey: app → CSRF → Session → AuthRateLimit → SecureHeaders
    # → TrustedProxy. TrustedProxy must be outermost so it strips untrusted
    # ``x-forwarded-*`` headers before any downstream middleware (rate
    # limiter, HSTS logic, _client_ip) sees them.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(AuthRateLimitMiddleware)
    app.add_middleware(SecureHeadersMiddleware)
    app.add_middleware(TrustedProxyMiddleware)

    # Auth + onboarding routes always register; they're inert in
    # SINGLE_TENANT mode because the SessionMiddleware short-circuits
    # before reaching them and /login itself redirects to /.
    app.include_router(auth_routes.router)
    app.include_router(onboarding_routes.router)

    app.include_router(home.router)
    app.include_router(today.router)
    app.include_router(worklists.router)
    app.include_router(tasks.router)
    app.include_router(templates_routes.router)
    app.include_router(alerts_routes.router)
    app.include_router(personnel.router)
    app.include_router(quals.router)
    app.include_router(absences.router)
    # Admin (Phase 2): invites, roles, members, workcenters. Each route
    # gates on its own org.* permission so members without admin perms
    # see 403 if they navigate here directly. SINGLE_TENANT mode opens
    # the gates so the AppImage can still administer its own DB.
    app.include_router(admin_routes.router)
    return app


app = create_app()


def _pick_free_port(host: str) -> int:
    """Ask the OS for an unused TCP port on *host*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]


# Browser binaries we'll try, in order, for an "app window" experience.
# All chromium-family browsers accept ``--app=URL`` to launch a single
# window without tabs/address bar — feels like a native app. Firefox
# doesn't have an exact equivalent; we fall through to the default
# browser in that case.
_CHROMIUM_BINARIES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "brave-browser",
    "microsoft-edge",
)


def _launch_app_window(url: str) -> None:
    """Open *url* in an app-style standalone window when possible.

    Tries chromium-family browsers with ``--app=URL`` first (gives a
    clean window with no tabs/address bar). Falls back to whatever
    ``webbrowser`` resolves to. The browser profile lives next to the
    app data dir so cookies/cache don't leak into the user's daily
    browser profile.
    """
    data_dir = Path(
        os.environ.get(
            "TBDTASK_DATA_DIR",
            Path.home() / ".local" / "share" / "tbdtask",
        )
    )
    profile_dir = data_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    for binary in _CHROMIUM_BINARIES:
        path = shutil.which(binary)
        if path is None:
            continue
        try:
            subprocess.Popen(
                [
                    path,
                    f"--app={url}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            continue

    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass


def serve(
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    """Entry point for the AppImage launcher.

    *port* defaults to a free OS-assigned port to avoid collisions when
    another instance — or anything else — already holds the previous
    default. The chosen port is printed to stdout so the user can
    re-open the app in another browser if needed.
    """
    import uvicorn

    if port is None:
        env_port = os.environ.get("TBDTASK_PORT")
        port = int(env_port) if env_port else _pick_free_port(host)

    url = f"http://{host}:{port}/"
    print(f"tbdtask listening on {url}", file=sys.stderr, flush=True)

    if open_browser:
        _launch_app_window(url)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
