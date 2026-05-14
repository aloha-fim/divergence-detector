"""Pydantic schemas — request bodies and API responses.

Kept in one file because they're small and the cross-references are easier
to follow than across many small modules.
"""
from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, ConfigDict, Field


# -------------------------------------------------------------------------
# Instruments
# -------------------------------------------------------------------------
class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    asset_class: str
    display_name: str
    active: bool


# -------------------------------------------------------------------------
# Divergence
# -------------------------------------------------------------------------
class DivergenceEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    instrument_id: int
    instrument_symbol: Optional[str] = None
    instrument_asset_class: Optional[str] = None
    implied_z: float
    realized_z: float
    divergence_score: float
    regime_label: str
    lookback_days: int


class DivergenceSeriesPoint(BaseModel):
    ts: datetime
    implied_z: float
    realized_z: float
    divergence_score: float


class NarrativeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    divergence_event_id: int
    generated_at: datetime
    model: str
    prompt_version: str
    body: str
    historical_analogs: Optional[list[dict]] = None
    latency_ms: Optional[int] = None


class AnalogOut(BaseModel):
    event_id: int
    ts: datetime
    instrument_symbol: str
    divergence_score: float
    regime_label: str
    label: Optional[str] = None
    similarity: float


# -------------------------------------------------------------------------
# Commentary
# -------------------------------------------------------------------------
class CommentaryIn(BaseModel):
    ts: Optional[datetime] = None
    source: Optional[str] = None
    raw_text: str
    instrument_symbols: Optional[list[str]] = None


class CommentaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    source: Optional[str]
    raw_text: str
    stress_score: Optional[float]
    sentiment_score: Optional[float]


# -------------------------------------------------------------------------
# Subscriptions
# -------------------------------------------------------------------------
class SubscriptionIn(BaseModel):
    name: str
    asset_classes: Optional[list[str]] = None
    instrument_ids: Optional[list[int]] = None
    min_abs_score: float = 2.0
    direction: Literal["positive", "negative", "either"] = "either"
    regime_labels: Optional[list[str]] = None
    channel: Literal["email", "webhook", "websocket"]
    webhook_url: Optional[str] = None
    cooldown_min: int = 60


class SubscriptionPatch(BaseModel):
    name: Optional[str] = None
    asset_classes: Optional[list[str]] = None
    instrument_ids: Optional[list[int]] = None
    min_abs_score: Optional[float] = None
    direction: Optional[Literal["positive", "negative", "either"]] = None
    regime_labels: Optional[list[str]] = None
    channel: Optional[Literal["email", "webhook", "websocket"]] = None
    webhook_url: Optional[str] = None
    cooldown_min: Optional[int] = None
    active: Optional[bool] = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    name: str
    asset_classes: Optional[list[str]]
    instrument_ids: Optional[list[int]]
    min_abs_score: float
    direction: str
    regime_labels: Optional[list[str]]
    channel: str
    webhook_url: Optional[str]
    cooldown_min: int
    active: bool
    created_at: datetime


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subscription_id: int
    divergence_event_id: int
    delivered_at: datetime
    channel: str
    status: str
    error: Optional[str]
