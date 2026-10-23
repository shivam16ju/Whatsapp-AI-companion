import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import init_db, SessionLocal
from app.scheduler import start_scheduler, shutdown_scheduler, sync_schedules_from_db
from app.routes.webhook import router as webhook_router
from app.routes.api import router as api_router
from app.routes.bridge import router as bridge_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wa-companion")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize database tables and background scheduler
    logger.info("Initializing database...")
    init_db()

    logger.info("Starting APScheduler...")
    start_scheduler()
    with SessionLocal() as db:
        sync_schedules_from_db(db)

    yield

    # Shutdown: clean up background scheduler
    logger.info("Shutting down APScheduler...")
    shutdown_scheduler()


app = FastAPI(
    title="WhatsApp AI Companion",
    description="WhatsApp AI Companion (Baby I Need Space) backend & webhook service",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles

# Mount API routers
app.include_router(webhook_router)
app.include_router(api_router)
app.include_router(bridge_router)

MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

DASHBOARD_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve single-page static dashboard."""
    if DASHBOARD_FILE.exists():
        return FileResponse(DASHBOARD_FILE)
    return HTMLResponse("<h2>Dashboard index.html not found. Please create dashboard/index.html</h2>", status_code=404)


@app.get("/qr", response_class=HTMLResponse)
async def proxy_bridge_qr():
    """Proxy WhatsApp Web QR linking page from bridge for public and local access."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{settings.BRIDGE_URL}/qr")
            return HTMLResponse(content=resp.text, status_code=resp.status_code)
    except Exception as e:
        return HTMLResponse(f"<div style='font-family: sans-serif; color: #f8fafc; background: #0f172a; padding: 20px; text-align: center;'><h3>Waiting for WhatsApp Bridge...</h3><p style='color: #94a3b8;'>({e})</p></div>", status_code=503)


@app.get("/status")
async def proxy_bridge_status():
    """Proxy WhatsApp Web bridge connection status."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{settings.BRIDGE_URL}/status")
            return resp.json()
    except Exception as e:
        return {"status": "disconnected", "error": str(e)}


@app.post("/reset")
async def proxy_bridge_reset():
    """Proxy WhatsApp Web session reset."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(f"{settings.BRIDGE_URL}/reset")
            return resp.json()
    except Exception as e:
        return {"success": False, "error": str(e)}
