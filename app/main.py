from __future__ import annotations

import webbrowser
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routes import home, personnel, quals, admin, absences, today, worklists, tasks, templates as templates_routes, alerts as alerts_routes


APP_ROOT = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="tbdtask", version="0.1.0")
    app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
    app.include_router(home.router)
    app.include_router(today.router)
    app.include_router(worklists.router)
    app.include_router(tasks.router)
    app.include_router(templates_routes.router)
    app.include_router(alerts_routes.router)
    app.include_router(personnel.router)
    app.include_router(quals.router)
    app.include_router(absences.router)
    app.include_router(admin.router)
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
