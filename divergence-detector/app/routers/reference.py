"""Instruments listing + standalone analog-finder endpoint.

The analog finder here is the "give me a date, find similar days" feature —
distinct from /divergence/{id}/analogs which is event-anchored.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Instrument
from app.schemas import AnalogOut, InstrumentOut
from app.services.analog_finder import find_analogs_by_date

router = APIRouter(tags=["reference"])


@router.get("/instruments", response_model=list[InstrumentOut])
async def list_instruments(
    active_only: bool = True,
    asset_class: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(Instrument)
    if active_only:
        q = q.where(Instrument.active.is_(True))
    if asset_class:
        q = q.where(Instrument.asset_class == asset_class)
    rows = (await db.execute(q.order_by(Instrument.symbol))).scalars().all()
    return list(rows)


@router.get("/analogs/search", response_model=list[AnalogOut])
async def search_analogs(
    instrument_symbol: str = Query(...),
    target_date: datetime = Query(...),
    k: int = Query(5, ge=1, le=20),
    exclude_window_days: int = Query(30, ge=0, le=365),
    db: AsyncSession = Depends(get_db),
):
    inst = (await db.execute(
        select(Instrument).where(Instrument.symbol == instrument_symbol)
    )).scalar_one_or_none()
    if not inst:
        raise HTTPException(404, "Instrument not found")

    rows = await find_analogs_by_date(db, inst.id, target_date, k, exclude_window_days)
    return [AnalogOut(**r) for r in rows]
