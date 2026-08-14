"""Customer endpoints.

Note the risk spread across four operations on one resource: reading is free,
editing contact details is a low-risk write, and deletion is the most dangerous
call in the whole system. Declaring risk per-operation rather than per-resource
is what lets the assistant allow a lookup and gate a delete in the same plan.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from nl2api.config import Role
from nl2api.mock_api.deps import require_role
from nl2api.mock_api.models import (
    Customer,
    CustomerStatus,
    CustomerUpdate,
    Page,
    Subscription,
)
from nl2api.mock_api.risk import RiskLevel, risk
from nl2api.mock_api.store import Store, get_store

router = APIRouter(prefix="/customers", tags=["customers"])

StoreDep = Annotated[Store, Depends(get_store)]
CustomerId = Annotated[str, Path(description="Customer identifier, e.g. CUS-1001.")]


@router.get(
    "",
    operation_id="search_customers",
    response_model=Page[Customer],
    summary="Search customers by email, name or status",
    description=(
        "Look up customers. Email matching is exact; name matching is a "
        "case-insensitive substring, so a partial name may return several "
        "customers. Use this to resolve a person to a customer_id before "
        "acting on their account."
    ),
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads customer records. Changes nothing.",
    idempotent=True,
)
async def search_customers(
    store: StoreDep,
    email: Annotated[str | None, Query(description="Exact email address to match.")] = None,
    name: Annotated[
        str | None, Query(description="Case-insensitive substring of the customer's name.")
    ] = None,
    status_filter: Annotated[
        CustomerStatus | None, Query(alias="status", description="Filter by lifecycle status.")
    ] = None,
) -> Page[Customer]:
    results = store.search_customers(email=email, name=name, status=status_filter)
    return Page(data=results, total=len(results))


@router.get(
    "/{customer_id}",
    operation_id="get_customer",
    response_model=Customer,
    summary="Fetch one customer by id",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads a single customer record. Changes nothing.",
    idempotent=True,
)
async def get_customer(store: StoreDep, customer_id: CustomerId) -> Customer:
    return store.get_customer(customer_id)


@router.get(
    "/{customer_id}/subscriptions",
    operation_id="list_customer_subscriptions",
    response_model=Page[Subscription],
    summary="List a customer's subscriptions",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads subscription records. Changes nothing.",
    idempotent=True,
)
async def list_customer_subscriptions(
    store: StoreDep, customer_id: CustomerId
) -> Page[Subscription]:
    results = store.list_subscriptions(customer_id)
    return Page(data=results, total=len(results))


@router.patch(
    "/{customer_id}",
    operation_id="update_customer",
    response_model=Customer,
    summary="Update a customer's contact details",
    description=(
        "Change name, email, company or country. Cannot change lifecycle "
        "status — use the subscription endpoints for that."
    ),
    dependencies=[Depends(require_role(Role.SUPPORT_AGENT))],
)
@risk(
    RiskLevel.LOW_RISK_WRITE,
    minimum_role=Role.SUPPORT_AGENT,
    side_effects="Overwrites the customer's contact details. Easily reverted.",
)
async def update_customer(
    store: StoreDep, customer_id: CustomerId, payload: CustomerUpdate
) -> Customer:
    return store.update_customer(customer_id, payload.model_dump(exclude_unset=True))


@router.delete(
    "/{customer_id}",
    operation_id="delete_customer",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently delete a customer",
    description=(
        "Hard-deletes the customer and their subscription history. Rejected "
        "while an active subscription exists. There is no undo."
    ),
    dependencies=[Depends(require_role(Role.BILLING_ADMIN))],
)
@risk(
    RiskLevel.HIGH_RISK_WRITE,
    minimum_role=Role.BILLING_ADMIN,
    side_effects=(
        "Permanently erases the customer and all of their subscription records. "
        "This cannot be undone and the data cannot be recovered."
    ),
)
async def delete_customer(store: StoreDep, customer_id: CustomerId) -> None:
    store.delete_customer(customer_id)
