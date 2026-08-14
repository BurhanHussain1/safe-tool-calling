"""Support ticket endpoints.

All three writes are low-risk: they create or annotate internal records and
change nothing a customer is billed. They execute without an approval gate but
are still reported back to the user, which is what ``low_risk_write`` means.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from nl2api.config import Role
from nl2api.mock_api.deps import require_role
from nl2api.mock_api.models import (
    Page,
    Ticket,
    TicketCommentCreate,
    TicketCreate,
    TicketStatus,
    TicketUpdate,
)
from nl2api.mock_api.risk import RiskLevel, risk
from nl2api.mock_api.store import Store, get_store

router = APIRouter(prefix="/tickets", tags=["tickets"])

StoreDep = Annotated[Store, Depends(get_store)]
TicketId = Annotated[str, Path(description="Ticket identifier, e.g. TIC-3001.")]


@router.get(
    "",
    operation_id="list_tickets",
    response_model=Page[Ticket],
    summary="List support tickets, optionally filtered",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads ticket records. Changes nothing.",
    idempotent=True,
)
async def list_tickets(
    store: StoreDep,
    customer_id: Annotated[
        str | None, Query(description="Only tickets for this customer, e.g. CUS-1001.")
    ] = None,
    status_filter: Annotated[
        TicketStatus | None, Query(alias="status", description="Filter by ticket status.")
    ] = None,
) -> Page[Ticket]:
    results = store.list_tickets(customer_id=customer_id, status=status_filter)
    return Page(data=results, total=len(results))


@router.get(
    "/{ticket_id}",
    operation_id="get_ticket",
    response_model=Ticket,
    summary="Fetch one ticket, including its comments",
)
@risk(
    RiskLevel.READ_ONLY,
    minimum_role=Role.VIEWER,
    side_effects="Reads a single ticket record. Changes nothing.",
    idempotent=True,
)
async def get_ticket(store: StoreDep, ticket_id: TicketId) -> Ticket:
    return store.get_ticket(ticket_id)


@router.post(
    "",
    operation_id="create_ticket",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    summary="Open a support ticket for a customer",
    description="Creates a ticket in the `open` state. The customer must exist.",
    dependencies=[Depends(require_role(Role.SUPPORT_AGENT))],
)
@risk(
    RiskLevel.LOW_RISK_WRITE,
    minimum_role=Role.SUPPORT_AGENT,
    side_effects="Creates an internal support ticket. No billing impact.",
)
async def create_ticket(store: StoreDep, payload: TicketCreate) -> Ticket:
    return store.create_ticket(payload.model_dump())


@router.patch(
    "/{ticket_id}",
    operation_id="update_ticket",
    response_model=Ticket,
    summary="Change a ticket's status, priority or assignee",
    description="A closed ticket must be reopened before any other field can change.",
    dependencies=[Depends(require_role(Role.SUPPORT_AGENT))],
)
@risk(
    RiskLevel.LOW_RISK_WRITE,
    minimum_role=Role.SUPPORT_AGENT,
    side_effects="Updates ticket metadata. No billing impact. Easily reverted.",
)
async def update_ticket(store: StoreDep, ticket_id: TicketId, payload: TicketUpdate) -> Ticket:
    return store.update_ticket(ticket_id, payload.model_dump(exclude_unset=True))


@router.post(
    "/{ticket_id}/comments",
    operation_id="add_ticket_comment",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment or internal note to a ticket",
    description=(
        "Set `internal: true` for a note the customer will not see. "
        "Rejected if the ticket is closed."
    ),
    dependencies=[Depends(require_role(Role.SUPPORT_AGENT))],
)
@risk(
    RiskLevel.LOW_RISK_WRITE,
    minimum_role=Role.SUPPORT_AGENT,
    side_effects=(
        "Appends a comment to the ticket. A non-internal comment is visible to "
        "the customer and cannot be unsent."
    ),
)
async def add_ticket_comment(
    store: StoreDep, ticket_id: TicketId, payload: TicketCommentCreate
) -> Ticket:
    return store.add_comment(ticket_id, payload.model_dump())
