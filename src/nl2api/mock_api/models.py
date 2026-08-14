"""Domain models for the mock SaaS admin system.

Two conventions that matter downstream:

* **Statuses are ``Literal``, never bare ``str``.** They become ``enum`` in the
  OpenAPI schema, which means the validator can reject an invalid status before
  a request is ever sent, and the planner can see the legal values.
* **Money is integer minor units** (cents). Floats do not belong anywhere near
  a refund amount.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --------------------------------------------------------------------------
# Status vocabularies
# --------------------------------------------------------------------------
CustomerStatus = Literal["active", "trial", "churned"]
SubscriptionPlan = Literal["free", "starter", "pro", "enterprise"]
SubscriptionStatus = Literal["active", "past_due", "canceled"]
InvoiceStatus = Literal["open", "paid", "void", "refunded", "partially_refunded"]
TicketStatus = Literal["open", "pending", "resolved", "closed"]
TicketPriority = Literal["low", "normal", "high", "urgent"]
RefundStatus = Literal["succeeded", "failed"]

Cents = Annotated[int, Field(ge=0, description="Amount in minor units (cents).")]

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Envelope for list endpoints.

    Results always live under ``data`` so that plan steps can reference them
    uniformly, e.g. ``$steps.s1.data[0].id``.
    """

    data: list[T]
    total: int = Field(description="Number of records matching the filter.")


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
class Customer(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"id": "CUS-1001"}})

    id: str = Field(description="Customer identifier, e.g. CUS-1001.")
    name: str
    email: EmailStr
    company: str
    country: str = Field(description="ISO 3166-1 alpha-2 country code.")
    status: CustomerStatus
    created_at: datetime


class CustomerUpdate(BaseModel):
    """Contact-detail changes. Deliberately cannot change ``status``.

    Lifecycle transitions belong to the subscription endpoints, so a support
    agent editing an email address can never accidentally churn an account.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    company: str | None = Field(default=None, min_length=1, max_length=120)
    country: str | None = Field(default=None, min_length=2, max_length=2)


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------
class Subscription(BaseModel):
    id: str = Field(description="Subscription identifier, e.g. SUB-2001.")
    customer_id: str
    plan: SubscriptionPlan
    status: SubscriptionStatus
    seats: int = Field(ge=1)
    mrr_cents: Cents = Field(description="Monthly recurring revenue in cents.")
    started_at: datetime
    renews_at: datetime | None = Field(default=None, description="Null once canceled.")
    canceled_at: datetime | None = None


class ChangePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: SubscriptionPlan = Field(description="The plan to move the subscription to.")
    seats: int | None = Field(default=None, ge=1, le=1000)
    effective: Literal["immediate", "next_cycle"] = "next_cycle"
    reason: str = Field(min_length=3, max_length=280, description="Why the plan is changing.")


class CancelSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=280)
    cancel_at: Literal["immediate", "period_end"] = "period_end"


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
class Invoice(BaseModel):
    id: str = Field(description="Invoice identifier, e.g. INV-1007.")
    customer_id: str
    subscription_id: str | None = None
    amount_cents: Cents
    refunded_cents: Cents = Field(default=0, description="Total already refunded.")
    currency: Literal["USD"] = "USD"
    status: InvoiceStatus
    issued_at: datetime
    due_at: datetime
    paid_at: datetime | None = None

    @property
    def refundable_cents(self) -> int:
        """How much of this invoice can still be refunded."""
        if self.status not in ("paid", "partially_refunded"):
            return 0
        return self.amount_cents - self.refunded_cents


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------
class TicketComment(BaseModel):
    id: str
    author: str
    body: str
    internal: bool = Field(description="Internal notes are not shown to the customer.")
    created_at: datetime


class Ticket(BaseModel):
    id: str = Field(description="Ticket identifier, e.g. TIC-3001.")
    customer_id: str
    subject: str
    body: str
    status: TicketStatus
    priority: TicketPriority
    assignee: str | None = None
    created_at: datetime
    updated_at: datetime
    comments: list[TicketComment] = Field(default_factory=list)


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(description="Customer the ticket is about, e.g. CUS-1001.")
    subject: str = Field(min_length=3, max_length=160)
    body: str = Field(min_length=3, max_length=4000)
    priority: TicketPriority = "normal"
    assignee: str | None = None


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assignee: str | None = None


class TicketCommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    internal: bool = False


# --------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------
class Refund(BaseModel):
    id: str = Field(description="Refund identifier, e.g. REF-4001.")
    invoice_id: str
    customer_id: str
    amount_cents: Cents
    currency: Literal["USD"] = "USD"
    reason: str
    status: RefundStatus
    created_at: datetime
    created_by: str


class RefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: str = Field(description="Invoice to refund against, e.g. INV-1007.")
    amount_cents: int = Field(
        gt=0,
        description="Amount to refund in cents. Cannot exceed the unrefunded balance.",
    )
    reason: str = Field(min_length=3, max_length=280)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")


class ErrorResponse(BaseModel):
    """Uniform error envelope so the executor can classify failures reliably."""

    error: ErrorBody
