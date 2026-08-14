"""Subscription endpoints.

Both writes here are high-risk because both change what the customer is billed.
Plan changes and cancellations are the classic "the agent did what?" incidents,
so they get dry-run previews and an approval gate in Phase 3.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from nl2api.config import Role
from nl2api.mock_api.deps import require_role
from nl2api.mock_api.models import (
    CancelSubscriptionRequest,
    ChangePlanRequest,
    Subscription,
)
from nl2api.mock_api.risk import RiskLevel, risk
from nl2api.mock_api.store import Store, get_store

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

StoreDep = Annotated[Store, Depends(get_store)]
SubscriptionId = Annotated[str, Path(description="Subscription identifier, e.g. SUB-2001.")]


@router.get(
    "/{subscription_id}",
    operation_id="get_subscription",
    response_model=Subscription,
    summary="Fetch one subscription by id",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads a single subscription record. Changes nothing.",
    idempotent=True,
)
async def get_subscription(store: StoreDep, subscription_id: SubscriptionId) -> Subscription:
    return store.get_subscription(subscription_id)


@router.post(
    "/{subscription_id}/change-plan",
    operation_id="change_subscription_plan",
    response_model=Subscription,
    summary="Move a subscription to a different plan",
    description=(
        "Changes plan and/or seat count, recalculating monthly recurring "
        "revenue. Rejected if the subscription is canceled, if nothing would "
        "actually change, or if the enterprise plan is requested with fewer "
        "than 10 seats."
    ),
    dependencies=[Depends(require_role(Role.BILLING_ADMIN))],
)
@risk(
    RiskLevel.HIGH_RISK_WRITE,
    minimum_role=Role.BILLING_ADMIN,
    side_effects=(
        "Changes what the customer is billed every month from the next cycle "
        "(or immediately). Reversible, but the customer will see the change."
    ),
)
async def change_subscription_plan(
    store: StoreDep, subscription_id: SubscriptionId, payload: ChangePlanRequest
) -> Subscription:
    return store.change_plan(
        subscription_id,
        plan=payload.plan,
        seats=payload.seats,
        effective=payload.effective,
    )


@router.post(
    "/{subscription_id}/cancel",
    operation_id="cancel_subscription",
    response_model=Subscription,
    summary="Cancel a subscription",
    description=(
        "Cancels at period end by default, or immediately if requested. "
        "Rejected if the subscription is already canceled."
    ),
    dependencies=[Depends(require_role(Role.BILLING_ADMIN))],
)
@risk(
    RiskLevel.HIGH_RISK_WRITE,
    minimum_role=Role.BILLING_ADMIN,
    side_effects=(
        "Ends the customer's paid service and stops future billing. "
        "Reinstating requires creating a new subscription."
    ),
)
async def cancel_subscription(
    store: StoreDep, subscription_id: SubscriptionId, payload: CancelSubscriptionRequest
) -> Subscription:
    return store.cancel_subscription(subscription_id, cancel_at=payload.cancel_at)
