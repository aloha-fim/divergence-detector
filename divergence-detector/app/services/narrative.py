"""LLM-driven narrative generation.

Calls Anthropic's Messages API (Claude Sonnet by default) with a versioned
prompt. Narratives are cached on (divergence_event_id, prompt_version) so
re-renders are free; `force_regenerate=True` writes a new row.

If USE_MOCK_LLM is set or no API key is available, falls back to a
deterministic templated narrative — useful for CI and offline demos.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import DivergenceEvent, Instrument, Narrative
from app.services.analog_finder import find_analogs

settings = get_settings()


SYSTEM_PROMPT = """You are a senior rates strategist writing for institutional PMs.
You analyze the gap between what markets are PRICING (implied) and what
they are EXECUTING (realized). You are concise, numerate, and never
hedge with filler. Three short paragraphs maximum. No bullet points.
No disclaimers. If the divergence is statistically marginal (|z|<1.5),
say so plainly rather than manufacturing significance."""


USER_TEMPLATE = """Date: {ts}
Instrument: {instrument} ({asset_class})

Today's signals:
- Implied stress z-score: {implied_z:+.2f}
- Realized stress z-score: {realized_z:+.2f}
- Divergence (implied - realized): {divergence:+.2f}
- Regime classification: {regime_label}

Cross-asset context (same date):
{cross_asset_table}

Recent dealer commentary (last 48h, LLM-classified stress score in parens):
{commentary_snippets}

Closest historical analogs by feature similarity:
{analog_table}

Write the brief. Address:
1. What is the market pricing vs what is executing? Be specific about magnitudes.
2. Which historical analog is the best fit and why — or note if none fit well.
3. What would invalidate the current read? (i.e., what to watch tomorrow.)

If divergence is positive and large: markets fear more than execution
validates — historically a fade signal but not always. If negative:
execution is stressed while implied signals are calm — historically
the more dangerous setup. Reflect this asymmetry honestly."""


# -------------------------------------------------------------------------
# Context loaders
# -------------------------------------------------------------------------
async def _cross_asset_table(session: AsyncSession, ts: datetime) -> str:
    rows = await session.execute(text("""
      SELECT i.symbol, e.implied_z, e.realized_z, e.divergence_score
      FROM divergence_events e
      JOIN instruments i ON i.id = e.instrument_id
      WHERE ABS(EXTRACT(EPOCH FROM (e.ts - :ts))) < 3600
      ORDER BY ABS(e.divergence_score) DESC
      LIMIT 8
    """), {"ts": ts})
    lines = ["  symbol            imp_z   rea_z   div"]
    for r in rows.mappings():
        lines.append(
            f"  {r['symbol']:<16} {r['implied_z']:+5.2f}  {r['realized_z']:+5.2f}  {r['divergence_score']:+5.2f}"
        )
    return "\n".join(lines) if len(lines) > 1 else "  (no cross-section available)"


async def _commentary_snippets(
    session: AsyncSession, ts: datetime, hours: int = 48, k: int = 8
) -> str:
    rows = await session.execute(text("""
      SELECT raw_text, source, stress_score
      FROM commentary
      WHERE ts BETWEEN :start AND :end
      ORDER BY ts DESC
      LIMIT :k
    """), {
        "start": ts - timedelta(hours=hours),
        "end": ts,
        "k": k,
    })
    lines = []
    for r in rows.mappings():
        score = r["stress_score"] if r["stress_score"] is not None else 0.0
        snippet = r["raw_text"][:140].replace("\n", " ")
        lines.append(f"  - [{r['source'] or 'unknown'} · {score:.2f}] {snippet}")
    return "\n".join(lines) if lines else "  (no recent commentary)"


def _analog_table(analogs: list[dict]) -> str:
    if not analogs:
        return "  (no analogs found)"
    lines = []
    for a in analogs:
        date = a["ts"].strftime("%Y-%m-%d")
        label = a.get("label") or "(unlabeled)"
        lines.append(
            f"  - {date}  sim={a['similarity']:.2f}  div={a['divergence_score']:+.2f}  "
            f"regime={a['regime_label']}  {label}"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------------
# Mock fallback (for local dev / CI)
# -------------------------------------------------------------------------
def _mock_narrative(event: DivergenceEvent, instrument: Instrument, analogs: list[dict]) -> str:
    direction = "execution stressed while implied signals are calm" if event.divergence_score < 0 \
        else "markets pricing more stress than execution validates"
    sigma = abs(event.divergence_score)
    severity = "marginal" if sigma < 1.5 else "elevated" if sigma < 2.5 else "extreme"

    analog_clause = ""
    if analogs:
        a = analogs[0]
        label = a.get("label") or a["ts"].strftime("%Y-%m-%d")
        analog_clause = f" The closest historical analog is {label}, at {a['similarity']:.2f} cosine similarity."

    return (
        f"{instrument.display_name} is showing a {severity} divergence "
        f"({event.divergence_score:+.2f}σ): {direction}. Implied composite "
        f"sits at {event.implied_z:+.2f}σ against a realized read of "
        f"{event.realized_z:+.2f}σ.{analog_clause}\n\n"
        f"Regime: {event.regime_label}. Watch for the two sides converging "
        f"tomorrow — either implied repricing into realized, or realized "
        f"normalizing back to implied — as the primary invalidator."
    )


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------
async def generate_narrative(
    session: AsyncSession,
    event_id: int,
    force_regenerate: bool = False,
) -> Narrative:
    """Get or generate the narrative for an event. Idempotent on
    (event_id, prompt_version) unless `force_regenerate`."""
    # Cache check
    if not force_regenerate:
        cached = (await session.execute(
            select(Narrative)
            .where(Narrative.divergence_event_id == event_id)
            .where(Narrative.prompt_version == settings.prompt_version)
        )).scalar_one_or_none()
        if cached:
            return cached

    event = (await session.execute(
        select(DivergenceEvent).where(DivergenceEvent.id == event_id)
    )).scalar_one_or_none()
    if not event:
        raise ValueError(f"No event {event_id}")

    instrument = event.instrument
    analogs = await find_analogs(session, event_id, k=5)
    cross_table = await _cross_asset_table(session, event.ts)
    commentary = await _commentary_snippets(session, event.ts)

    user_msg = USER_TEMPLATE.format(
        ts=event.ts.strftime("%Y-%m-%d"),
        instrument=instrument.symbol,
        asset_class=instrument.asset_class,
        implied_z=event.implied_z,
        realized_z=event.realized_z,
        divergence=event.divergence_score,
        regime_label=event.regime_label,
        cross_asset_table=cross_table,
        commentary_snippets=commentary,
        analog_table=_analog_table(analogs),
    )

    t0 = time.perf_counter()

    if settings.use_mock_llm or not settings.anthropic_api_key:
        body = _mock_narrative(event, instrument, analogs)
        model = "mock"
    else:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.llm_model,
            max_tokens=600,
            temperature=settings.llm_temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        body = resp.content[0].text
        model = settings.llm_model

    latency_ms = int((time.perf_counter() - t0) * 1000)

    narrative = Narrative(
        divergence_event_id=event_id,
        model=model,
        prompt_version=settings.prompt_version,
        body=body,
        historical_analogs=[
            {
                "event_id": a["event_id"],
                "ts": a["ts"].isoformat(),
                "instrument_symbol": a["instrument_symbol"],
                "similarity": float(a["similarity"]),
                "label": a.get("label"),
            }
            for a in analogs
        ],
        latency_ms=latency_ms,
    )

    # On force_regenerate, replace the existing row for this prompt_version
    if force_regenerate:
        await session.execute(text("""
          DELETE FROM narratives
          WHERE divergence_event_id = :eid AND prompt_version = :pv
        """), {"eid": event_id, "pv": settings.prompt_version})

    session.add(narrative)
    await session.commit()
    await session.refresh(narrative)
    return narrative
