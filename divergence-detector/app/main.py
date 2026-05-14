"""FastAPI application entrypoint.

Lifespan manages the APScheduler — start on app boot, stop cleanly on
shutdown. CORS is wide-open by default for the dev UI; tighten in prod.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import commentary, divergence, reference, subscriptions, ws
from app.workers import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s · %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting divergence-api")
    start_scheduler()
    try:
        yield
    finally:
        logger.info("stopping divergence-api")
        stop_scheduler()


app = FastAPI(
    title="Divergence Detector API",
    description="Realized vs implied liquidity intelligence for U.S. rates markets.",
    version="0.7.2",
    lifespan=lifespan,
)

# CORS
origins = ["*"] if settings.cors_origins == "*" else settings.cors_origins.split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(reference.router)
app.include_router(divergence.router)
app.include_router(commentary.router)
app.include_router(subscriptions.router)
app.include_router(ws.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.7.2"}
