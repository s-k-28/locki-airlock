"""FastAPI entry point.

    uv run uvicorn app.main:app --reload            # dev (frontend on :5173)
    uv run python -m app.main                        # serves frontend/dist too, if built

Binds to 127.0.0.1 by default: the API can move a robot, so it must not be
exposed on shared hackathon Wi-Fi without authentication.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router
from .api.websocket import handle_ws
from .config import PROJECT_ROOT, Settings
from .runtime import LockedInRuntime

_PROCESS_T0 = time.perf_counter()
log = logging.getLogger("lockedin")


def create_app(settings: Settings | None = None, runtime: LockedInRuntime | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.offline:
        # Model libraries must not try the network on the robot's Wi-Fi: every model is local
        # (backend/models, checked by scripts/offline_check.py).
        for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
            os.environ.setdefault(var, "1")
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or LockedInRuntime(settings)
        app.state.runtime = rt
        await rt.start()
        startup_ms = (time.perf_counter() - _PROCESS_T0) * 1000
        rt.perf.add("backend_startup_ms", startup_ms)
        log.info("LOCKED IN %s ready in %.0f ms (driver=%s)", __version__, startup_ms, rt.driver)
        try:
            yield
        finally:
            await rt.shutdown()

    app = FastAPI(title="LOCKED IN", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await handle_ws(ws, app.state.runtime)

    dist = PROJECT_ROOT / "frontend" / "dist"
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=Path(dist), html=True), name="frontend")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = Settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level=s.log_level.lower())
