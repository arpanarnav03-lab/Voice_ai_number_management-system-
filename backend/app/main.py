from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, poller
from .bolna_client import BolnaError, RealBolnaClient
from .config import Settings
from .db import Database
from .errors import AppError
from .fake_bolna import SimulatedBolna

log = logging.getLogger("bnm")
DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def create_app(settings: Settings | None = None, bolna=None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if bolna is None:
        bolna = RealBolnaClient(settings.bolna_api_key) if settings.bolna_mode == "real" else SimulatedBolna()
    db = db or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_migrations:
            db.migrate()
        else:
            db.create_all()
        task = asyncio.create_task(poller.poll_loop(app)) if settings.start_poller else None
        log.info("Bolna Number Manager started in '%s' mode", settings.bolna_mode)
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await bolna.aclose()
            db.dispose()

    app = FastAPI(title="Bolna Number Manager", lifespan=lifespan)
    app.state.settings = settings
    app.state.bolna = bolna
    app.state.db = db

    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return JSONResponse(e.body(), status_code=e.status)

    @app.exception_handler(BolnaError)
    async def _bolna_error(_: Request, e: BolnaError):
        return JSONResponse({"code": "BOLNA_ERROR", "message": f"Bolna: {e.message}"}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"code": "VALIDATION_ERROR", "message": f"Invalid input{': ' + loc if loc else ''}"},
                            status_code=422)

    app.include_router(api.router)

    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"code": "NOT_FOUND", "message": "Not found"}, status_code=404)
            f = DIST / path
            return FileResponse(f if path and f.is_file() else DIST / "index.html")

    return app
