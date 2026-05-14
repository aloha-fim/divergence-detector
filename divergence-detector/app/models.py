"""SQLAlchemy ORM models.

These mirror the schema in init_db.sql. We use Mapped/mapped_column style for
type-safety and don't drive DDL from here — the SQL file is the source of truth
because of the TimescaleDB hypertables and pgvector indexes that aren't well
expressed in ORM metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey,
    Integer, Numeric, String, Text, UniqueConstraint, Index, JSON
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String, unique=True)
    asset_class: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    api_key: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImpliedMetric(Base):
    __tablename__ = "implied_metrics"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    metric_type: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float]
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class RealizedMetric(Base):
    __tablename__ = "realized_metrics"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    t_cost_bps: Mapped[Optional[float]]
    intraday_vol_bps: Mapped[Optional[float]]
    composite_width_bps: Mapped[Optional[float]]
    benchmark_size: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)


class Commentary(Base):
    __tablename__ = "commentary"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    stress_score: Mapped[Optional[float]]
    sentiment_score: Mapped[Optional[float]]
    instrument_ids: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1536), nullable=True)


class DivergenceEvent(Base):
    __tablename__ = "divergence_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    implied_z: Mapped[float]
    realized_z: Mapped[float]
    divergence_score: Mapped[float]
    regime_label: Mapped[str] = mapped_column(String)
    lookback_days: Mapped[int] = mapped_column(default=252)
    feature_vector: Mapped[Optional[list[float]]] = mapped_column(Vector(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    instrument: Mapped["Instrument"] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("ts", "instrument_id", "lookback_days"),
    )


class EventLabel(Base):
    __tablename__ = "event_labels"

    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("divergence_events.id"), primary_key=True)
    label: Mapped[str] = mapped_column(String)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Narrative(Base):
    __tablename__ = "narratives"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    divergence_event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("divergence_events.id"))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    model: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    historical_analogs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[Optional[int]]

    __table_args__ = (
        UniqueConstraint("divergence_event_id", "prompt_version"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String)
    asset_classes: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)
    instrument_ids: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), nullable=True)
    min_abs_score: Mapped[float] = mapped_column(default=2.0)
    direction: Mapped[str] = mapped_column(String, default="either")
    regime_labels: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)
    channel: Mapped[str] = mapped_column(String)
    webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cooldown_min: Mapped[int] = mapped_column(default=60)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("subscriptions.id"))
    divergence_event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("divergence_events.id"))
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    channel: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("subscription_id", "divergence_event_id"),
    )
