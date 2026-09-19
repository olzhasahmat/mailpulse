"""HTTP API: бэкенд Mini App и раздача собранного фронтенда."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from mailpulse.api.routes import router
from mailpulse.config import get_settings
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.observability import setup_logging

log = logging.getLogger(__name__)
MINIAPP_DIST = Path(__file__).resolve().parents[3] / "miniapp" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine()
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="MailPulse API", version="0.1.0", lifespan=lifespan)
    # Mini App грузится с домена Telegram; в проде фронт раздаётся отсюда же
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", response_model=None)
    async def ready(request: Request) -> dict[str, str] | JSONResponse:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse(
                {"status": "db_unavailable", "error": type(exc).__name__}, status_code=503
            )
        return {"status": "ok"}

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    if not (MINIAPP_DIST / "index.html").exists():
        log.warning("miniapp/dist не собран — фронтенд не раздаётся (make miniapp)")
        return
    app.mount("/assets", StaticFiles(directory=MINIAPP_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(MINIAPP_DIST / "index.html")


def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        create_app(),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
