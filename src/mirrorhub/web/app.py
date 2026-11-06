"""FastAPI application and scheduler lifecycle."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from mirrorhub.db import db_ok, get_engine
from mirrorhub.logging_conf import configure_logging
from mirrorhub.security import auth
from mirrorhub.security.headers import SecurityHeadersMiddleware
from mirrorhub.services import presets, scheduler
from mirrorhub.settings import get_settings
from mirrorhub.web.deps import NotAuthenticated, render, security_context
from mirrorhub.web.routes import audit, dashboard
from mirrorhub.web.routes import auth as auth_routes
from mirrorhub.web.routes import config as config_routes
from mirrorhub.web.routes import exports as export_routes
from mirrorhub.web.routes import jobs as job_routes
from mirrorhub.web.routes import keys as key_routes
from mirrorhub.web.routes import notifications as notification_routes
from mirrorhub.web.routes import presets as preset_routes
from mirrorhub.web.routes import repos as repo_routes
from mirrorhub.web.routes import schedules as schedule_routes
from mirrorhub.web.routes import settings as settings_routes
from mirrorhub.web.routes import storage as storage_routes

log = logging.getLogger("mirrorhub.web")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()

    with Session(get_engine()) as db:
        generated = auth.bootstrap_admin(db, settings)
        if generated:
            auth.print_generated_password(settings.admin_username, generated)
        purged = auth.purge_expired_sessions(db)
        seeded = presets.seed(db)
        scheduler.ensure_next_fire(db)

    log.info(
        "web started",
        extra={"expired_sessions_purged": purged, "presets_seeded": seeded},
    )
    if not (STATIC_DIR / "htmx.min.js").exists():
        log.warning("vendored htmx.min.js is missing")

    task = asyncio.create_task(
        scheduler.run_forever(),
        name="mirrorhub-scheduler",
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _not_authenticated(request: Request, _exc: Exception) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if request.headers.get("hx-request") or exc.status_code == 401:
        return Response(
            str(exc.detail),
            status_code=exc.status_code,
            media_type="text/plain",
        )

    if not hasattr(request.state, "csrf_secret"):
        request.state.csrf_secret = None
        request.state.user = None

    return render(
        request,
        "error.html",
        {"status": exc.status_code, "detail": str(exc.detail)},
        exc.status_code,
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_format, settings.log_level)
    app = FastAPI(
        title="MirrorHub",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(NotAuthenticated, _not_authenticated)
    app.add_exception_handler(StarletteHTTPException, _http_error)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        if db_ok():
            return Response("ok", media_type="text/plain")
        return Response(
            "database unavailable",
            status_code=503,
            media_type="text/plain",
        )

    dependencies = [Depends(security_context)]
    for module in (
        auth_routes,
        dashboard,
        repo_routes,
        job_routes,
        export_routes,
        schedule_routes,
        storage_routes,
        preset_routes,
        key_routes,
        config_routes,
        notification_routes,
        settings_routes,
        audit,
    ):
        app.include_router(module.router, dependencies=dependencies)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
