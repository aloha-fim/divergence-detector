"""Subscription CRUD + delivery history.

Auth is intentionally minimal here — a simple API key header that maps to a
user. In production you'd swap in JWT or OAuth.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Subscription, User
from app.schemas import (
    DeliveryOut, SubscriptionIn, SubscriptionOut, SubscriptionPatch,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


# -------------------------------------------------------------------------
# Auth dependency
# -------------------------------------------------------------------------
async def current_user(
    x_api_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = (await db.execute(
        select(User).where(User.api_key == x_api_key)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "Invalid API key")
    return user


# -------------------------------------------------------------------------
# CRUD
# -------------------------------------------------------------------------
@router.post("/", response_model=SubscriptionOut, status_code=201)
async def create(
    payload: SubscriptionIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.channel == "webhook" and not payload.webhook_url:
        raise HTTPException(400, "webhook_url required when channel=webhook")

    sub = Subscription(
        user_id=user.id,
        name=payload.name,
        asset_classes=payload.asset_classes,
        instrument_ids=payload.instrument_ids,
        min_abs_score=payload.min_abs_score,
        direction=payload.direction,
        regime_labels=payload.regime_labels,
        channel=payload.channel,
        webhook_url=payload.webhook_url,
        cooldown_min=payload.cooldown_min,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.get("/", response_model=list[SubscriptionOut])
async def list_mine(
    active_only: bool = True,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(Subscription).where(Subscription.user_id == user.id)
    if active_only:
        q = q.where(Subscription.active.is_(True))
    rows = (await db.execute(q.order_by(Subscription.created_at.desc()))).scalars().all()
    return list(rows)


@router.patch("/{sub_id}", response_model=SubscriptionOut)
async def update(
    sub_id: int,
    patch: SubscriptionPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id, Subscription.user_id == user.id)
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "Subscription not found")

    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(sub, field, value)

    await db.commit()
    await db.refresh(sub)
    return sub


@router.delete("/{sub_id}", status_code=204)
async def delete(
    sub_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(text("""
      DELETE FROM subscriptions WHERE id = :id AND user_id = :uid
    """), {"id": sub_id, "uid": user.id})
    if res.rowcount == 0:
        raise HTTPException(404, "Subscription not found")
    await db.commit()


@router.get("/{sub_id}/deliveries", response_model=list[DeliveryOut])
async def history(
    sub_id: int,
    limit: int = Query(50, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    # Confirm ownership
    own = (await db.execute(
        select(Subscription.id)
        .where(Subscription.id == sub_id, Subscription.user_id == user.id)
    )).scalar_one_or_none()
    if not own:
        raise HTTPException(404, "Subscription not found")

    rows = await db.execute(text("""
      SELECT * FROM alert_deliveries
      WHERE subscription_id = :id
      ORDER BY delivered_at DESC
      LIMIT :lim
    """), {"id": sub_id, "lim": limit})
    return [DeliveryOut(**dict(r)) for r in rows.mappings()]
