"""Divergence scoring.

Pulls implied + realized metrics from the last N days, computes per-component
rolling z-scores, aggregates to a composite per side (weighted), and persists
divergence_events with a 32-d feature vector for downstream pgvector analog
lookup.

The composite weights are read from `metric_weights` (per asset class), so
PMs can tune the signal without redeploying — this is a knob that matters
once you have live data and discover, for example, that MOVE dominates
T-Cost noise for Treasuries but not for MBS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    DivergenceEvent, Instrument, ImpliedMetric, RealizedMetric,
)

settings = get_settings()

# Higher value = MORE stress for each metric type. T-Cost & vol are
# naturally directional (higher = more stress); for sentiment we invert
# during normalization in `_load_implied`.
STRESS_DIRECTION_IMPLIED = {
    "option_iv": +1, "move": +1, "vix": +1,
    "dealer_sent": -1,  # lower sentiment = more stress, so invert
}
STRESS_DIRECTION_REALIZED = {
    "t_cost_bps": +1, "intraday_vol_bps": +1, "composite_width_bps": +1,
}


@dataclass
class ScoringResult:
    ts: datetime
    instrument_id: int
    implied_z: float
    realized_z: float
    divergence_score: float
    regime_label: str
    feature_vector: list[float]


# -------------------------------------------------------------------------
# Loading
# -------------------------------------------------------------------------
async def _load_implied(
    session: AsyncSession, instrument_id: int, since: datetime
) -> pd.DataFrame:
    """Pivot implied_metrics into a ts-indexed frame of metric_type columns,
    sign-flipped so higher always = more stress."""
    q = (
        select(ImpliedMetric.ts, ImpliedMetric.metric_type, ImpliedMetric.value)
        .where(ImpliedMetric.instrument_id == instrument_id)
        .where(ImpliedMetric.ts >= since)
    )
    rows = (await session.execute(q)).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "metric_type", "value"])
    df = df.pivot(index="ts", columns="metric_type", values="value").sort_index()
    for col in df.columns:
        sign = STRESS_DIRECTION_IMPLIED.get(col, +1)
        df[col] = df[col] * sign
    return df


async def _load_realized(
    session: AsyncSession, instrument_id: int, since: datetime
) -> pd.DataFrame:
    q = (
        select(
            RealizedMetric.ts,
            RealizedMetric.t_cost_bps,
            RealizedMetric.intraday_vol_bps,
            RealizedMetric.composite_width_bps,
        )
        .where(RealizedMetric.instrument_id == instrument_id)
        .where(RealizedMetric.ts >= since)
        .order_by(RealizedMetric.ts)
    )
    rows = (await session.execute(q)).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows, columns=["ts", "t_cost_bps", "intraday_vol_bps", "composite_width_bps"]
    ).set_index("ts").sort_index()
    return df


# -------------------------------------------------------------------------
# Core math
# -------------------------------------------------------------------------
def _rolling_z(s: pd.Series, w: int) -> pd.Series:
    """Trailing rolling z-score with `w` lookback (uses min_periods=w//2)."""
    mu = s.rolling(w, min_periods=max(20, w // 2)).mean()
    sd = s.rolling(w, min_periods=max(20, w // 2)).std()
    return (s - mu) / sd.replace(0, np.nan)


def _composite_z(df: pd.DataFrame, lookback: int) -> pd.Series:
    """Mean of per-column rolling z-scores. Drop columns with insufficient
    history rather than poisoning the average."""
    if df.empty:
        return pd.Series(dtype=float)
    zs = []
    for col in df.columns:
        z = _rolling_z(df[col].astype(float), lookback)
        if z.notna().sum() > 20:
            zs.append(z)
    if not zs:
        return pd.Series(dtype=float)
    return pd.concat(zs, axis=1).mean(axis=1)


def _classify_regime(implied_z: float, realized_z: float) -> str:
    """Three buckets: calm / stressed / dislocated.

    'Stressed' = both sides elevated together (the system is consistently
    pricing what it executes). 'Dislocated' = the two diverge sharply, which
    is the actually interesting regime."""
    a, r = abs(implied_z), abs(realized_z)
    div = abs(implied_z - realized_z)
    if div > 2.5:
        return "dislocated"
    if a > 1.5 or r > 1.5:
        return "stressed"
    return "calm"


def _build_feature_vector(
    today_implied: float,
    today_realized: float,
    cross_section: dict[str, dict],
) -> list[float]:
    """Pack a 32-d vector capturing the day's *shape*.

    See services/analog_finder.py for the layout — kept in sync there. We
    handcraft features here rather than embedding the raw time series because:
      - small dim → ivfflat is cheap
      - features carry semantic meaning, so neighbors are interpretable
      - asset-class structure is preserved without one-hot blowup
    """
    asset_class_order = ["rates", "mbs", "credit", "swaps", "equity_vol", "broad"]
    v = np.zeros(32, dtype=np.float32)

    # [0:6] implied z by asset class (mean if multiple instruments)
    # [6:12] realized z by asset class
    # [12:18] divergence by asset class
    for i, ac in enumerate(asset_class_order):
        rows = [r for r in cross_section.values() if r["asset_class"] == ac]
        if rows:
            v[0 + i] = float(np.mean([r["implied_z"] for r in rows]))
            v[6 + i] = float(np.mean([r["realized_z"] for r in rows]))
            v[12 + i] = float(np.mean([r["divergence"] for r in rows]))

    all_imp = np.array([r["implied_z"] for r in cross_section.values()])
    all_rea = np.array([r["realized_z"] for r in cross_section.values()])
    all_div = np.array([r["divergence"] for r in cross_section.values()])

    # [18:22] dispersion
    if len(all_imp):
        v[18] = float(all_imp.std())
        v[19] = float(all_rea.std())
        v[20] = float(np.max(np.abs(all_div)))
        v[21] = float(np.mean(np.abs(all_div) > 2))

    # [22:26] sign pattern
    if len(all_imp):
        v[22] = float(np.mean(all_imp > 0))
        v[23] = float(np.mean(all_rea > 0))
        v[24] = float(np.mean(all_div > 0))
        v[25] = float(today_implied - today_realized)  # this instrument's signed div

    # [26:28] this instrument's own z-scores (anchor)
    v[26] = today_implied
    v[27] = today_realized

    # [28:32] reserved — kept zero for now, future room for regime indicators
    return v.tolist()


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------
async def score_instrument(
    session: AsyncSession,
    instrument_id: int,
    as_of: datetime,
    lookback_days: int | None = None,
    cross_section: dict[str, dict] | None = None,
) -> ScoringResult | None:
    """Compute a single divergence event for one instrument at `as_of`.

    `cross_section` is the same-day {symbol: {asset_class, implied_z,
    realized_z, divergence}} mapping; if not provided, the feature vector
    falls back to a degenerate single-instrument encoding (still valid but
    less useful for analog search).
    """
    lookback = lookback_days or settings.default_lookback_days
    since = as_of - timedelta(days=lookback * 2)  # buffer for warmup

    imp_df = await _load_implied(session, instrument_id, since)
    rea_df = await _load_realized(session, instrument_id, since)

    imp_z = _composite_z(imp_df, lookback)
    rea_z = _composite_z(rea_df, lookback)

    # align to as_of (or latest available before)
    imp_at = imp_z.asof(as_of) if not imp_z.empty else np.nan
    rea_at = rea_z.asof(as_of) if not rea_z.empty else np.nan

    if pd.isna(imp_at) or pd.isna(rea_at):
        return None

    div = float(imp_at - rea_at)
    regime = _classify_regime(float(imp_at), float(rea_at))

    cs = cross_section or {}
    fv = _build_feature_vector(float(imp_at), float(rea_at), cs)

    return ScoringResult(
        ts=as_of,
        instrument_id=instrument_id,
        implied_z=float(imp_at),
        realized_z=float(rea_at),
        divergence_score=div,
        regime_label=regime,
        feature_vector=fv,
    )


async def score_all_instruments(
    session: AsyncSession, as_of: datetime, lookback_days: int | None = None
) -> list[DivergenceEvent]:
    """Score every active instrument at `as_of` and persist.

    Two-pass: first pass computes per-instrument z-scores without cross-section
    context, second pass builds the cross-section dict and re-derives the
    feature vector. Slight cost, much better analog vectors.
    """
    instruments = (
        await session.execute(select(Instrument).where(Instrument.active.is_(True)))
    ).scalars().all()

    # Pass 1: per-instrument z
    prelim: dict[int, tuple[Instrument, ScoringResult]] = {}
    for inst in instruments:
        res = await score_instrument(session, inst.id, as_of, lookback_days)
        if res:
            prelim[inst.id] = (inst, res)

    # Build cross-section
    cs = {
        inst.symbol: {
            "asset_class": inst.asset_class,
            "implied_z": r.implied_z,
            "realized_z": r.realized_z,
            "divergence": r.divergence_score,
        }
        for inst, r in prelim.values()
    }

    # Pass 2: rebuild feature vectors with full cross-section, persist
    persisted: list[DivergenceEvent] = []
    for inst, prelim_r in prelim.values():
        fv = _build_feature_vector(prelim_r.implied_z, prelim_r.realized_z, cs)
        # Upsert by (ts, instrument_id, lookback_days)
        await session.execute(text("""
          INSERT INTO divergence_events
            (ts, instrument_id, implied_z, realized_z, divergence_score,
             regime_label, lookback_days, feature_vector)
          VALUES (:ts, :inst, :iz, :rz, :div, :reg, :lb, :fv)
          ON CONFLICT (ts, instrument_id, lookback_days) DO UPDATE SET
            implied_z = EXCLUDED.implied_z,
            realized_z = EXCLUDED.realized_z,
            divergence_score = EXCLUDED.divergence_score,
            regime_label = EXCLUDED.regime_label,
            feature_vector = EXCLUDED.feature_vector
        """), {
            "ts": as_of, "inst": inst.id, "iz": prelim_r.implied_z,
            "rz": prelim_r.realized_z, "div": prelim_r.divergence_score,
            "reg": prelim_r.regime_label,
            "lb": lookback_days or settings.default_lookback_days,
            "fv": str(fv),  # pgvector accepts string repr
        })

    await session.commit()

    # Re-select what we just wrote so callers get persisted rows with IDs
    result = await session.execute(
        select(DivergenceEvent)
        .where(DivergenceEvent.ts == as_of)
        .where(DivergenceEvent.instrument_id.in_(list(prelim.keys())))
    )
    return list(result.scalars().all())
