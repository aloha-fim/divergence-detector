"""Scheduled scoring + dispatch loop.

Runs every N minutes during market hours: scores all instruments at the
current ts, identifies events crossing the alert threshold, and dispatches.

Production note: APScheduler runs in-process, which is fine at this scale.
At higher throughput swap for Celery beat + a worker pool so scoring runs
independently of the web tier.
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db import SessionLocal
from app.services.dispatch import dispatch_event
from app.services.scoring import score_all_instruments

logger = logging.getLogger(__name__)
settings = get_settings()

scheduler = AsyncIOScheduler()


async def scoring_tick() -> None:
    """One pass: score, then dispatch any high-|z| events."""
    now = datetime.utcnow()
    logger.info("scoring_tick start ts=%s", now.isoformat())

    async with SessionLocal() as db:
        try:
            events = await score_all_instruments(db, now)
        except Exception:
            logger.exception("scoring failed")
            return

        # Dispatch only events crossing the alert threshold; the scoring
        # itself writes everything for the historical record.
        flagged = [e for e in events if abs(e.divergence_score) >= settings.alert_threshold_z]
        logger.info("scored=%d flagged=%d", len(events), len(flagged))

        for event in flagged:
            try:
                counts = await dispatch_event(db, event)
                logger.info("dispatch event=%s counts=%s", event.id, counts)
            except Exception:
                logger.exception("dispatch failed event=%s", event.id)


def start_scheduler() -> None:
    """Attach the periodic job. Called from app lifespan."""
    scheduler.add_job(
        scoring_tick,
        trigger=IntervalTrigger(minutes=settings.scoring_interval_minutes),
        id="scoring_tick",
        replace_existing=True,
        max_instances=1,  # never overlap a slow run with the next tick
    )
    scheduler.start()
    logger.info("scheduler started · interval=%dm", settings.scoring_interval_minutes)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
