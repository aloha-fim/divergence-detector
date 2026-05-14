"""Divergence event endpoints + narrative + analogs.

Mirrors the UI: /current for the dashboard grid, /series for the pulse
chart, /{id} for the detail view, /{id}/narrative for the AI brief,
/{id}/analogs for the historical neighbors panel.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import DivergenceEvent, Instrument, Narrative
from app.schemas import (
    AnalogOut, DivergenceEventOut, DivergenceSeriesPoint, NarrativeOut,
)
from app.services.analog_finder import find_analogs
from app.services.narrative import generate_narrative

router = APIRouter(prefix="/divergence", tags=["divergence"])


@router.get("/current", response_model=list[DivergenceEventOut])
async def current(
    min_abs_score: float = Query(0.0, ge=0.0),
    asset_class: Optional[str] = None,
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Latest event per instrument, optionally filtered. Powers the
    dashboard grid."""
    sql = text("""
      SELECT DISTINCT ON (e.instrument_id)
        e.id, e.ts, e.instrument_id,
        i.symbol AS instrument_symbol,
        i.asset_class AS instrument_asset_class,
        e.implied_z, e.realized_z, e.divergence_score,
        e.regime_label, e.lookback_days
      FROM divergence_events e
      JOIN instruments i ON i.id = e.instrument_id
      WHERE i.active
        AND ABS(e.divergence_score) >= :min_abs
        AND (:ac IS NULL OR i.asset_class = :ac)
      ORDER BY e.instrument_id, e.ts DESC
      LIMIT :lim
    """)
    rows = await db.execute(sql, {
        "min_abs": min_abs_score,
        "ac": asset_class,
        "lim": limit,
    })
    return [DivergenceEventOut(**dict(r)) for r in rows.mappings()]


@router.get("/series", response_model=list[DivergenceSeriesPoint])
async def series(
    instrument_symbol: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """Time series for the pulse chart. If `instrument_symbol` omitted,
    returns the mean across all active instruments."""
    since = since or (datetime.utcnow() - timedelta(days=30))
    until = until or datetime.utcnow()

    if instrument_symbol:
        sql = text("""
          SELECT e.ts, e.implied_z, e.realized_z, e.divergence_score
          FROM divergence_events e
          JOIN instruments i ON i.id = e.instrument_id
          WHERE i.symbol = :sym AND e.ts BETWEEN :s AND :u
          ORDER BY e.ts
        """)
        params = {"sym": instrument_symbol, "s": since, "u": until}
    else:
        sql = text("""
          SELECT
            date_trunc('day', e.ts) AS ts,
            AVG(e.implied_z)        AS implied_z,
            AVG(e.realized_z)       AS realized_z,
            AVG(e.divergence_score) AS divergence_score
          FROM divergence_events e
          JOIN instruments i ON i.id = e.instrument_id
          WHERE i.active AND e.ts BETWEEN :s AND :u
          GROUP BY date_trunc('day', e.ts)
          ORDER BY 1
        """)
        params = {"s": since, "u": until}

    rows = await db.execute(sql, params)
    return [DivergenceSeriesPoint(**dict(r)) for r in rows.mappings()]


@router.get("/{event_id}", response_model=DivergenceEventOut)
async def get_event(event_id: int, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(text("""
      SELECT e.id, e.ts, e.instrument_id,
             i.symbol AS instrument_symbol,
             i.asset_class AS instrument_asset_class,
             e.implied_z, e.realized_z, e.divergence_score,
             e.regime_label, e.lookback_days
      FROM divergence_events e
      JOIN instruments i ON i.id = e.instrument_id
      WHERE e.id = :eid
    """), {"eid": event_id})).mappings().first()
    if not row:
        raise HTTPException(404, "Event not found")
    return DivergenceEventOut(**dict(row))


@router.post("/{event_id}/narrative", response_model=NarrativeOut)
async def narrative(
    event_id: int,
    force_regenerate: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Get-or-generate. Cached on (event_id, prompt_version)."""
    try:
        n = await generate_narrative(db, event_id, force_regenerate)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return n


@router.get("/{event_id}/analogs", response_model=list[AnalogOut])
async def analogs(
    event_id: int,
    k: int = Query(5, ge=1, le=20),
    exclude_window_days: int = Query(30, ge=0, le=365),
    same_asset_class_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    rows = await find_analogs(db, event_id, k, exclude_window_days, same_asset_class_only)
    return [AnalogOut(**r) for r in rows]
