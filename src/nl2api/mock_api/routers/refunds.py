"""Refund endpoints.

``create_refund`` is the single money-moving operation in the system and the
centrepiece of the demo: it is high-risk, non-idempotent, billing-admin only,
and its four business rules each become a golden test case.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from nl2api.config import Role
from nl2api.mock_api.deps import CurrentActor, require_role
from nl2api.mock_api.models import Page, Refund, RefundCreate
from nl2api.mock_api.risk import RiskLevel, risk
from nl2api.mock_api.store import Store, get_store

router = APIRouter(prefix="/refunds", tags=["refunds"])

StoreDep = Annotated[Store, Depends(get_store)]


@router.get(
    "",
    operation_id="list_refunds",
    response_model=Page[Refund],
    summary="List refunds, optionally for one invoice",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads refund records. Changes nothing.",
    idempotent=True,
)
async def list_refunds(
    store: StoreDep,
    invoice_id: Annotated[
        str | None, Query(description="Only refunds against this invoice, e.g. INV-1007.")
    ] = None,
) -> Page[Refund]:
    results = store.list_refunds(invoice_id=invoice_id)
    return Page(data=results, total=len(results))


@router.post(
    "",
    operation_id="create_refund",
    response_model=Refund,
    status_code=status.HTTP_201_CREATED,
    summary="Refund money against a paid invoice",
    description=(
        "Moves money back to the customer. The invoice must be `paid` or "
        "`partially_refunded`, and the amount must not exceed the unrefunded "
        "balance (`amount_cents - refunded_cents`). The invoice moves to "
        "`partially_refunded`, or to `refunded` once fully refunded.\n\n"
        "This operation is **not idempotent** — calling it twice refunds twice."
    ),
    dependencies=[Depends(require_role(Role.BILLING_ADMIN))],
)
@risk(
    RiskLevel.HIGH_RISK_WRITE,
    minimum_role=Role.BILLING_ADMIN,
    side_effects=(
        "Moves real money back to the customer and changes the invoice status. "
        "This cannot be undone from this API, and calling it twice refunds twice."
    ),
    idempotent=False,
)
async def create_refund(store: StoreDep, payload: RefundCreate, actor: CurrentActor) -> Refund:
    return store.create_refund(
        invoice_id=payload.invoice_id,
        amount_cents=payload.amount_cents,
        reason=payload.reason,
        actor=actor,
    )
