from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, poller, services, spam, sync
from .actionlog import ActionLog, ActionLogMiddleware
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
    db.session_info["alloc"] = services.AllocationRules(
        idle_first=settings.idle_first_enabled, idle_hours=settings.idle_hours,
        cooldown_hours=settings.number_move_cooldown_hours, test_expiry_hours=settings.test_result_expiry_hours)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_migrations:
            db.migrate()
        else:
            db.create_all()
        spam.recover_releasing(db)
        with db.session() as s:  # self-heal: the stack must hold exactly the non-spam pool numbers
            fixed = services.reconcile_stack(s)
        if fixed["added"] or fixed["removed"] or fixed["duplicates_fixed"]:
            log.warning("startup repaired the test stack: %d added, %d removed, %d duplicate positions",
                        fixed["added"], fixed["removed"], fixed["duplicates_fixed"])
        task = asyncio.create_task(poller.poll_loop(app)) if settings.start_poller else None
        sync_task = asyncio.create_task(sync.sync_loop(app)) if settings.start_poller else None
        log.info("Bolna Number Manager started in '%s' mode", settings.bolna_mode)
        try:
            yield
        finally:
            for t in (task, sync_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            await bolna.aclose()
            db.dispose()
            if action_log:
                action_log.close()  # flush the record of changing requests

    app = FastAPI(title="Bolna Number Manager", lifespan=lifespan)
    action_log = ActionLog(settings.action_log_path) if settings.action_log_path else None
    if action_log:
        app.add_middleware(ActionLogMiddleware, log=action_log)
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
