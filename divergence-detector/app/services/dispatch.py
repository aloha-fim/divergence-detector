"""Alert dispatcher.

Given a fresh divergence_event, finds matching subscriptions, applies
cooldown, generates the narrative on-demand, routes to the appropriate
channel, and logs delivery for audit + de-dup.

The `alert_deliveries` UNIQUE (subscription_id, divergence_event_id)
constraint is the de-dup guarantee — even if scoring reruns or the
worker replays, a user can't receive the same alert twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DivergenceEvent, Instrument, Subscription
from app.services.narrative import generate_narrative
from app.services.ws_manager import manager as ws_manager

logger = logging.getLogger(__name__)


async def _find_matching_subs(
    session: AsyncSession, event: DivergenceEvent, instrument: Instrument
) -> list[Subscription]:
    """Two-step: raw SQL filters by array-membership conditions that ORM
    doesn't express cleanly, then re-fetch as proper ORM objects so typed
    attribute access works in the dispatch loop."""
    id_rows = await session.execute(text("""
      SELECT s.id
      FROM subscriptions s
      WHERE s.active
        AND (s.instrument_ids IS NULL OR :inst = ANY(s.instrument_ids))
        AND (s.asset_classes IS NULL OR :ac = ANY(s.asset_classes))
        AND (s.regime_labels IS NULL OR :regime = ANY(s.regime_labels))
        AND ABS(:score) >= s.min_abs_score
        AND (
          s.direction = 'either'
          OR (s.direction = 'positive' AND :score > 0)
          OR (s.direction = 'negative' AND :score < 0)
        )
    """), {
        "inst": instrument.id,
        "ac": instrument.asset_class,
        "regime": event.regime_label,
        "score": event.divergence_score,
    })
    sub_ids = [r[0] for r in id_rows.all()]
    if not sub_ids:
        return []

    result = await session.execute(
        select(Subscription).where(Subscription.id.in_(sub_ids))
    )
    return list(result.scalars().all())


async def _in_cooldown(
    session: AsyncSession, sub: Subscription, instrument_id: int, now: datetime
) -> bool:
    row = await session.execute(text("""
      SELECT 1 FROM alert_deliveries d
      JOIN divergence_events e ON e.id = d.divergence_event_id
      WHERE d.subscription_id = :sub
        AND e.instrument_id = :inst
        AND d.status = 'sent'
        AND d.delivered_at > :since
      LIMIT 1
    """), {
        "sub": sub.id,
        "inst": instrument_id,
        "since": now - timedelta(minutes=sub.cooldown_min),
    })
    return row.first() is not None


# -------------------------------------------------------------------------
# Channel delivery
# -------------------------------------------------------------------------
async def _deliver_email(sub: Subscription, payload: dict) -> None:
    # Placeholder — wire to SES / Postmark / Resend in production
    logger.info("EMAIL → user=%s event=%s", sub.user_id, payload["event_id"])


async def _deliver_webhook(sub: Subscription, payload: dict) -> None:
    if not sub.webhook_url:
        raise ValueError("webhook_url required for webhook channel")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(sub.webhook_url, json=payload)
        resp.raise_for_status()


async def _deliver_ws(sub: Subscription, payload: dict) -> None:
    sent = await ws_manager.send_to_user(sub.user_id, payload)
    if sent == 0:
        logger.info("WS → user=%s offline, no live socket", sub.user_id)


CHANNEL_HANDLERS = {
    "email": _deliver_email,
    "webhook": _deliver_webhook,
    "websocket": _deliver_ws,
}


async def dispatch_event(
    session: AsyncSession, event: DivergenceEvent
) -> dict[str, int]:
    """Process all subscriptions for one event. Returns counts by status."""
    counts = {"sent": 0, "suppressed_cooldown": 0, "failed": 0, "duplicate": 0}
    instrument = event.instrument
    now = datetime.utcnow()

    subs = await _find_matching_subs(session, event, instrument)
    if not subs:
        return counts

    narrative = await generate_narrative(session, event.id)

    payload = {
        "type": "divergence_alert",
        "event_id": event.id,
        "ts": event.ts.isoformat(),
        "instrument": instrument.symbol,
        "asset_class": instrument.asset_class,
        "divergence_score": event.divergence_score,
        "implied_z": event.implied_z,
        "realized_z": event.realized_z,
        "regime_label": event.regime_label,
        "narrative": narrative.body,
        "analogs": narrative.historical_analogs,
    }

    for sub in subs:
        if await _in_cooldown(session, sub, instrument.id, now):
            counts["suppressed_cooldown"] += 1
            await _log_delivery(session, sub.id, event.id, sub.channel,
                                "suppressed_cooldown", None)
            continue

        try:
            handler = CHANNEL_HANDLERS[sub.channel]
            await handler(sub, payload)
            await _log_delivery(session, sub.id, event.id, sub.channel, "sent", None)
            counts["sent"] += 1
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.exception("Delivery failed sub=%s event=%s", sub.id, event.id)
            try:
                await _log_delivery(session, sub.id, event.id, sub.channel, "failed", err)
                counts["failed"] += 1
            except Exception:
                counts["duplicate"] += 1

    await session.commit()
    return counts


async def _log_delivery(
    session: AsyncSession,
    sub_id: int,
    event_id: int,
    channel: str,
    status: str,
    error: str | None,
) -> None:
    await session.execute(text("""
      INSERT INTO alert_deliveries
        (subscription_id, divergence_event_id, channel, status, error)
      VALUES (:sub, :eid, :ch, :st, :err)
      ON CONFLICT (subscription_id, divergence_event_id) DO NOTHING
    """), {
        "sub": sub_id, "eid": event_id, "ch": channel,
        "st": status, "err": error,
    })
