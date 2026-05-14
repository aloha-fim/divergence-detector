"""Historical analog finder.

Given a divergence_event, returns the K nearest neighbors in feature space,
excluding any within `exclude_window_days` of the query event (you don't want
yesterday as your analog). Uses pgvector's cosine distance with the ivfflat
index for sublinear search.

Curated `event_labels` are joined in so the LLM narrative can name the analog
("SVB weekend", "GSE re-entry") rather than dump a date.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def find_analogs(
    session: AsyncSession,
    event_id: int,
    k: int = 5,
    exclude_window_days: int = 30,
    same_asset_class_only: bool = False,
) -> list[dict]:
    """Return the top-k nearest historical events.

    Each row: {event_id, ts, instrument_symbol, divergence_score,
               regime_label, label, similarity}
    """
    q = text("""
      WITH target AS (
        SELECT e.id, e.ts, e.feature_vector, i.asset_class
        FROM divergence_events e
        JOIN instruments i ON i.id = e.instrument_id
        WHERE e.id = :event_id
      )
      SELECT
        e.id            AS event_id,
        e.ts            AS ts,
        i.symbol        AS instrument_symbol,
        e.divergence_score,
        e.regime_label,
        l.label         AS label,
        1 - (e.feature_vector <=> target.feature_vector) AS similarity
      FROM divergence_events e
      JOIN instruments i  ON i.id = e.instrument_id
      LEFT JOIN event_labels l ON l.event_id = e.id
      CROSS JOIN target
      WHERE e.id <> target.id
        AND ABS(EXTRACT(EPOCH FROM (e.ts - target.ts))) > :exclude_secs
        AND e.feature_vector IS NOT NULL
        AND (:same_ac IS FALSE OR i.asset_class = target.asset_class)
      ORDER BY e.feature_vector <=> target.feature_vector
      LIMIT :k
    """)

    rows = await session.execute(q, {
        "event_id": event_id,
        "exclude_secs": exclude_window_days * 86400,
        "same_ac": same_asset_class_only,
        "k": k,
    })
    return [dict(r) for r in rows.mappings().all()]


async def find_analogs_by_date(
    session: AsyncSession,
    instrument_id: int,
    target_date: datetime,
    k: int = 5,
    exclude_window_days: int = 30,
) -> list[dict]:
    """Convenience: find the closest event to `target_date` for `instrument`,
    then return its analogs. Used by the standalone Analog Finder view."""
    result = await session.execute(text("""
      SELECT id FROM divergence_events
      WHERE instrument_id = :inst
        AND ABS(EXTRACT(EPOCH FROM (ts - :target))) < 86400
      ORDER BY ABS(EXTRACT(EPOCH FROM (ts - :target)))
      LIMIT 1
    """), {"inst": instrument_id, "target": target_date})
    row = result.first()
    if not row:
        return []
    return await find_analogs(session, row[0], k, exclude_window_days)
