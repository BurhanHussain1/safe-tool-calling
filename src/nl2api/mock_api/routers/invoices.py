"""Invoice endpoints — read-only.

There is deliberately no way to edit or void an invoice through this API. The
only thing that can change an invoice is a refund, which keeps the money-moving
surface down to exactly one operation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from nl2api.config import Role
from nl2api.mock_api.models import Invoice, InvoiceStatus, Page
from nl2api.mock_api.risk import RiskLevel, risk
from nl2api.mock_api.store import Store, get_store

router = APIRouter(prefix="/invoices", tags=["invoices"])

StoreDep = Annotated[Store, Depends(get_store)]


@router.get(
    "",
    operation_id="list_invoices",
    response_model=Page[Invoice],
    summary="List invoices, optionally filtered by customer and status",
    description=(
        "Returns invoices newest-first, so the most recent invoice is the first "
        "element of `data`. Filter by customer_id to find a specific account's "
        "billing history, or by status to find unpaid or refunded invoices."
    ),
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads invoice records. Changes nothing.",
    idempotent=True,
)
async def list_invoices(
    store: StoreDep,
    customer_id: Annotated[
        str | None, Query(description="Only invoices for this customer, e.g. CUS-1001.")
    ] = None,
    status_filter: Annotated[
        InvoiceStatus | None, Query(alias="status", description="Filter by invoice status.")
    ] = None,
) -> Page[Invoice]:
    results = store.list_invoices(customer_id=customer_id, status=status_filter)
    return Page(data=results, total=len(results))


@router.get(
    "/{invoice_id}",
    operation_id="get_invoice",
    response_model=Invoice,
    summary="Fetch one invoice by id",
    description=(
        "Includes `refunded_cents`, so the remaining refundable balance is "
        "`amount_cents - refunded_cents`."
    ),
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads a single invoice record. Changes nothing.",
    idempotent=True,
)
async def get_invoice(
    store: StoreDep,
    invoice_id: Annotated[str, Path(description="Invoice identifier, e.g. INV-1007.")],
) -> Invoice:
    return store.get_invoice(invoice_id)
