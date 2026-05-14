"""Seed the database with synthetic data matching the UI demo.

Generates ~2 years of daily implied + realized metrics for 6 instruments,
runs scoring across the full history (so the dashboard has data on first
open and pgvector has neighbors to search), labels several historically
significant events, and creates a demo user with three subscriptions.

Run: docker compose exec api python -m app.seed
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.db import SessionLocal
from app.models import (
    DivergenceEvent, EventLabel, Instrument, Subscription, User,
)
from app.services.scoring import score_all_instruments

logging.basicConfig(level=logging.INFO, format="%(asctime)s · %(message)s")
logger = logging.getLogger(__name__)

rng = np.random.default_rng(42)


INSTRUMENTS = [
    ("UST_10Y",    "rates",  "UST 10Y"),
    ("UST_30Y",    "rates",  "UST 30Y"),
    ("FNCL_30Y",   "mbs",    "FNCL 30Y · TBA"),
    ("CDX_IG",     "credit", "CDX Investment Grade"),
    ("CDX_HY",     "credit", "CDX High Yield"),
    ("USD_SW_5Y",  "swaps",  "USD Swap 5Y"),
]


async def _seed_instruments(db):
    for sym, ac, name in INSTRUMENTS:
        await db.execute(text("""
          INSERT INTO instruments (symbol, asset_class, display_name)
          VALUES (:s, :a, :n)
          ON CONFLICT (symbol) DO NOTHING
        """), {"s": sym, "a": ac, "n": name})
    await db.commit()


def _synthesize_implied(days: int) -> dict[str, np.ndarray]:
    """Two implied components, calm baseline with three stress episodes."""
    n = days
    move = 60 + np.cumsum(rng.normal(0, 1.2, n)) * 0.5
    sent = rng.normal(0, 1, n)

    # Stress events: COVID-style, SVB-style, recent
    for center, width, mag in [(int(n * 0.15), 30, 35), (int(n * 0.55), 14, 18), (n - 6, 5, 12)]:
        for i in range(max(0, center - width), min(n, center + width)):
            decay = np.exp(-((i - center) ** 2) / (width ** 2))
            move[i] += mag * decay
            sent[i] -= mag * decay * 0.05

    return {"move": np.clip(move, 30, None), "dealer_sent": sent}


def _synthesize_realized(days: int, ac: str, with_jan9_spike: bool) -> dict[str, np.ndarray]:
    """Realized metrics with asset-class-specific behavior. The MBS series
    gets a sharp recent spike to recreate the Jan 9 + today demo."""
    n = days
    base_cost = {"rates": 0.4, "mbs": 0.7, "credit": 0.3, "swaps": 0.5}[ac]
    t_cost = base_cost + np.abs(rng.normal(0, 0.15, n))
    vol = 5 + np.abs(rng.normal(0, 1.5, n))
    width = base_cost * 1.5 + np.abs(rng.normal(0, 0.2, n))

    # Stress episodes (mostly correlated with implied)
    for center, w, mag in [(int(n * 0.15), 25, 3.0), (int(n * 0.55), 10, 1.5)]:
        for i in range(max(0, center - w), min(n, center + w)):
            decay = np.exp(-((i - center) ** 2) / (w ** 2))
            t_cost[i] += mag * decay
            vol[i] += mag * 4 * decay
            width[i] += mag * 0.6 * decay

    # The Jan 9 MBS-only spike (and today's repeat)
    if with_jan9_spike:
        for i in range(n - 4, n):
            t_cost[i] += 3.5
            vol[i] += 8
            width[i] += 1.4

    return {
        "t_cost_bps": t_cost,
        "intraday_vol_bps": vol,
        "composite_width_bps": width,
    }


async def _seed_metrics(db):
    rows = await db.execute(text("SELECT id, symbol, asset_class FROM instruments"))
    instruments = [(r[0], r[1], r[2]) for r in rows.all()]

    days = 504  # ~2 years of business days
    end = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)

    for inst_id, sym, ac in instruments:
        timestamps = [end - timedelta(days=days - 1 - i) for i in range(days)]
        implied = _synthesize_implied(days)
        realized = _synthesize_realized(days, ac, with_jan9_spike=(sym == "FNCL_30Y"))

        # Bulk insert implied
        imp_rows = []
        for i, ts in enumerate(timestamps):
            for mt, arr in implied.items():
                imp_rows.append({"ts": ts, "inst": inst_id, "mt": mt, "val": float(arr[i])})
        # Chunk to avoid huge single statements
        for chunk_start in range(0, len(imp_rows), 1000):
            chunk = imp_rows[chunk_start:chunk_start + 1000]
            await db.execute(text("""
              INSERT INTO implied_metrics (ts, instrument_id, metric_type, value, source)
              VALUES (:ts, :inst, :mt, :val, 'seed')
              ON CONFLICT DO NOTHING
            """), chunk)

        # Bulk insert realized
        rea_rows = [
            {
                "ts": ts, "inst": inst_id,
                "tc": float(realized["t_cost_bps"][i]),
                "iv": float(realized["intraday_vol_bps"][i]),
                "cw": float(realized["composite_width_bps"][i]),
                "sz": 500_000_000,
            }
            for i, ts in enumerate(timestamps)
        ]
        for chunk_start in range(0, len(rea_rows), 1000):
            chunk = rea_rows[chunk_start:chunk_start + 1000]
            await db.execute(text("""
              INSERT INTO realized_metrics
                (ts, instrument_id, t_cost_bps, intraday_vol_bps,
                 composite_width_bps, benchmark_size)
              VALUES (:ts, :inst, :tc, :iv, :cw, :sz)
              ON CONFLICT DO NOTHING
            """), chunk)

        logger.info("metrics seeded · %s (%d days)", sym, days)

    await db.commit()


async def _score_history(db):
    """Score weekly (every 5 trading days) across the history, plus daily
    for the last 30 days. Faster than scoring every single day, still
    gives pgvector enough neighbors."""
    end = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)

    weekly = [end - timedelta(days=d) for d in range(60, 504, 5)]
    daily = [end - timedelta(days=d) for d in range(0, 30)]
    targets = sorted(set(weekly + daily))

    logger.info("scoring %d historical points...", len(targets))
    for i, ts in enumerate(targets):
        await score_all_instruments(db, ts)
        if (i + 1) % 25 == 0:
            logger.info("  %d/%d", i + 1, len(targets))
    logger.info("scoring complete")


async def _label_events(db):
    """Curated labels for the demo's most important analogs."""
    end = datetime.now(timezone.utc)
    targets = [
        (end - timedelta(days=int(504 * 0.85)), "FNCL_30Y", "covid_mar_2020", "COVID liquidity dislocation"),
        (end - timedelta(days=int(504 * 0.45)), "FNCL_30Y", "svb_mar_2023",   "SVB weekend — rates flight"),
        (end - timedelta(days=130),             "FNCL_30Y", "gse_jan_2026",   "GSE $200B MBS purchase order"),
    ]
    for ts, sym, label, desc in targets:
        await db.execute(text("""
          INSERT INTO event_labels (event_id, label, description)
          SELECT e.id, :label, :desc
          FROM divergence_events e
          JOIN instruments i ON i.id = e.instrument_id
          WHERE i.symbol = :sym
            AND ABS(EXTRACT(EPOCH FROM (e.ts - :ts))) < 86400 * 3
          ORDER BY ABS(EXTRACT(EPOCH FROM (e.ts - :ts)))
          LIMIT 1
          ON CONFLICT DO NOTHING
        """), {"ts": ts, "sym": sym, "label": label, "desc": desc})
    await db.commit()
    logger.info("event labels seeded")


async def _seed_user_and_subs(db):
    # Demo user
    await db.execute(text("""
      INSERT INTO users (email, display_name, api_key)
      VALUES ('demo@divergence.local', 'Demo', 'demo-api-key-change-me')
      ON CONFLICT (email) DO NOTHING
    """))
    user_id = (await db.execute(
        text("SELECT id FROM users WHERE email = 'demo@divergence.local'")
    )).scalar_one()

    subs = [
        {
            "name": "Hidden execution stress", "direction": "negative",
            "min_abs_score": 2.5, "channel": "websocket", "cooldown_min": 60,
            "regime_labels": ["dislocated"],
        },
        {
            "name": "Rates fade signals", "direction": "positive",
            "min_abs_score": 2.0, "channel": "webhook",
            "webhook_url": "http://localhost:9999/dev-null", "cooldown_min": 120,
            "asset_classes": ["rates", "swaps"],
        },
        {
            "name": "Credit early warning", "direction": "either",
            "min_abs_score": 1.8, "channel": "websocket", "cooldown_min": 240,
            "asset_classes": ["credit"], "active": False,
        },
    ]
    for s in subs:
        await db.execute(text("""
          INSERT INTO subscriptions
            (user_id, name, asset_classes, regime_labels, min_abs_score,
             direction, channel, webhook_url, cooldown_min, active)
          VALUES (:uid, :name, :ac, :rl, :ms, :dir, :ch, :wh, :cd, :a)
          ON CONFLICT DO NOTHING
        """), {
            "uid": user_id, "name": s["name"],
            "ac": s.get("asset_classes"), "rl": s.get("regime_labels"),
            "ms": s["min_abs_score"], "dir": s["direction"],
            "ch": s["channel"], "wh": s.get("webhook_url"),
            "cd": s["cooldown_min"], "a": s.get("active", True),
        })
    await db.commit()
    logger.info("demo user + 3 subscriptions seeded · api_key=demo-api-key-change-me")


async def main():
    async with SessionLocal() as db:
        logger.info("→ instruments")
        await _seed_instruments(db)

        logger.info("→ metrics (this takes ~30s)")
        await _seed_metrics(db)

        logger.info("→ historical scoring (this takes ~2min)")
        await _score_history(db)

        logger.info("→ event labels")
        await _label_events(db)

        logger.info("→ user + subscriptions")
        await _seed_user_and_subs(db)

    logger.info("✓ seed complete")


if __name__ == "__main__":
    asyncio.run(main())
