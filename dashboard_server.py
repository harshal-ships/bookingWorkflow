"""FastAPI server for the live booking dashboard."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dashboard_state import dashboard_events

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DASHBOARD_DIR = BASE_DIR / "dashboard"

app = FastAPI(title="XanhSM Live Call Dashboard")
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


@app.get("/")
async def dashboard_index() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/events")
async def events() -> list[dict]:
    return await dashboard_events.snapshot()


@app.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = await dashboard_events.subscribe()
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        logger.debug("Dashboard WebSocket disconnected")
    except asyncio.CancelledError:
        raise
    finally:
        await dashboard_events.unsubscribe(queue)


async def start_dashboard_server(host: str, port: int) -> None:
    """Run uvicorn inside the existing asyncio application."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Dashboard available at http://%s:%s", host, port)
    await server.serve()
