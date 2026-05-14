"""Commentary ingestion.

Accepts raw text from dealer chat / research / news, classifies it via LLM
(stress score + sentiment), embeds it for analog lookup, and writes the
row. Stress scores feed the narrative prompt's "recent commentary" section.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Commentary, Instrument
from app.schemas import CommentaryIn, CommentaryOut

router = APIRouter(prefix="/commentary", tags=["commentary"])


@router.post("/", response_model=CommentaryOut, status_code=201)
async def ingest(payload: CommentaryIn, db: AsyncSession = Depends(get_db)):
    """Classify + embed + persist. In production the LLM call is here;
    for now we stub with simple heuristics so this is runnable offline.
    Swap `_score_text` for an actual Anthropic call to wire up real
    classification."""
    ts = payload.ts or datetime.now(timezone.utc)
    stress, sentiment = _score_text(payload.raw_text)

    instrument_ids = None
    if payload.instrument_symbols:
        rows = (await db.execute(
            select(Instrument.id).where(Instrument.symbol.in_(payload.instrument_symbols))
        )).scalars().all()
        instrument_ids = list(rows)

    row = Commentary(
        ts=ts,
        source=payload.source,
        raw_text=payload.raw_text,
        stress_score=stress,
        sentiment_score=sentiment,
        instrument_ids=instrument_ids,
        model_version="heuristic-v1",
        embedding=None,  # set by background job in production
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# -------------------------------------------------------------------------
# Stub classifier
# -------------------------------------------------------------------------
STRESS_TERMS = {
    "illiquid": 0.6, "dislocation": 0.8, "freeze": 0.9, "wider": 0.4,
    "squeeze": 0.6, "stressed": 0.7, "panic": 0.95, "tail": 0.5,
    "concern": 0.4, "deteriorat": 0.6, "spike": 0.5,
}
CALM_TERMS = {"orderly": -0.5, "muted": -0.4, "stable": -0.4, "tight": -0.3}


def _score_text(text: str) -> tuple[float, float]:
    """Returns (stress 0..1, sentiment -1..1). Heuristic only."""
    t = text.lower()
    stress = 0.0
    for term, w in STRESS_TERMS.items():
        if term in t:
            stress = max(stress, w)
    sent = 0.0
    for term, w in CALM_TERMS.items():
        if term in t:
            sent = max(sent, abs(w))  # calm = positive
    for term, w in STRESS_TERMS.items():
        if term in t:
            sent = -w
            break
    return stress, max(-1.0, min(1.0, sent))
