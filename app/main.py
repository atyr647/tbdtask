from __future__ import annotations

import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
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


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Entry point for the AppImage launcher."""
    import uvicorn

    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{port}/", new=2)
        except Exception:
            pass
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
