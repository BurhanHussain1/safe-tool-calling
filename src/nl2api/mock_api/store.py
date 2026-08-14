"""In-memory datastore with a deterministic seed.

Business rules live here rather than in the routers. Routers translate HTTP to
intent; the store decides whether the intent is legal. That split means the
rules are testable without a client, and a rule can never be enforced on one
route and forgotten on another.

The seed is fixed — same IDs, amounts and timestamps on every run — because the
golden workflow suite in Phase 5 asserts against specific records. :meth:`Store.reset`
restores it, and the test suite calls that between cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nl2api.mock_api.models import (
    Customer,
    Invoice,
    Refund,
    Subscription,
    Ticket,
    TicketComment,
)

#: Anchor for every seeded timestamp. Fixed so tests never depend on the clock.
SEED_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _days(n: int) -> timedelta:
    return timedelta(days=n)


class StoreError(Exception):
    """A business rule said no.

    Carries the HTTP status and a stable machine-readable code so the router can
    translate without re-deciding anything.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class NotFound(StoreError):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(404, f"{resource}_not_found", f"No {resource} with id {identifier!r}.")


class Conflict(StoreError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class Unprocessable(StoreError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(422, code, message)


class Store:
    """The whole business system, in four dictionaries."""

    def __init__(self) -> None:
        self.customers: dict[str, Customer] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.invoices: dict[str, Invoice] = {}
        self.tickets: dict[str, Ticket] = {}
        self.refunds: dict[str, Refund] = {}
        self._counters: dict[str, int] = {}
        self.reset()

    # -- lifecycle ---------------------------------------------------------
    def reset(self) -> None:
        """Restore the seed. Called between tests for isolation."""
        self.customers = {c.id: c for c in _seed_customers()}
        self.subscriptions = {s.id: s for s in _seed_subscriptions()}
        self.invoices = {i.id: i for i in _seed_invoices()}
        self.tickets = {t.id: t for t in _seed_tickets()}
        self.refunds = {}
        self._counters = {"ticket": 3003, "comment": 5002, "refund": 4000}

    def snapshot(self) -> dict[str, Any]:
        """A comparable copy of all state.

        Phase 3 uses this to prove that generating a dry-run preview mutates
        nothing: snapshot, preview, snapshot, assert equal.
        """
        return {
            "customers": {k: v.model_dump(mode="json") for k, v in self.customers.items()},
            "subscriptions": {k: v.model_dump(mode="json") for k, v in self.subscriptions.items()},
            "invoices": {k: v.model_dump(mode="json") for k, v in self.invoices.items()},
            "tickets": {k: v.model_dump(mode="json") for k, v in self.tickets.items()},
            "refunds": {k: v.model_dump(mode="json") for k, v in self.refunds.items()},
        }

    def _next_id(self, kind: str, prefix: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{prefix}-{self._counters[kind]}"

    # -- customers ---------------------------------------------------------
    def get_customer(self, customer_id: str) -> Customer:
        try:
            return self.customers[customer_id]
        except KeyError:
            raise NotFound("customer", customer_id) from None

    def search_customers(
        self,
        *,
        email: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ) -> list[Customer]:
        """Filter customers. Email match is exact; name match is a substring.

        Substring name matching is intentional: it is what makes ``"refund Ana's
        invoice"`` ambiguous when two customers are named Ana, which is exactly
        the case the assistant must refuse to guess at.
        """
        results = list(self.customers.values())
        if email:
            needle = email.strip().lower()
            results = [c for c in results if c.email.lower() == needle]
        if name:
            needle = name.strip().lower()
            results = [c for c in results if needle in c.name.lower()]
        if status:
            results = [c for c in results if c.status == status]
        return sorted(results, key=lambda c: c.id)

    def update_customer(self, customer_id: str, changes: dict[str, Any]) -> Customer:
        customer = self.get_customer(customer_id)
        applied = {k: v for k, v in changes.items() if v is not None}
        if not applied:
            raise Unprocessable("empty_update", "Provide at least one field to change.")
        if "email" in applied:
            taken = [
                c
                for c in self.customers.values()
                if c.id != customer_id and c.email.lower() == str(applied["email"]).lower()
            ]
            if taken:
                raise Conflict(
                    "email_taken",
                    f"Email {applied['email']!r} already belongs to {taken[0].id}.",
                )
        updated = customer.model_copy(update=applied)
        self.customers[customer_id] = updated
        return updated

    def delete_customer(self, customer_id: str) -> None:
        """Hard-delete a customer.

        Blocked while an active subscription exists — deleting a paying customer
        by accident is the kind of thing this whole project is built to prevent.
        """
        self.get_customer(customer_id)
        active = [
            s
            for s in self.subscriptions.values()
            if s.customer_id == customer_id and s.status == "active"
        ]
        if active:
            raise Conflict(
                "customer_has_active_subscription",
                f"Customer {customer_id} still has an active subscription "
                f"({active[0].id}). Cancel it first.",
            )
        del self.customers[customer_id]
        for sub_id in [s.id for s in self.subscriptions.values() if s.customer_id == customer_id]:
            del self.subscriptions[sub_id]

    # -- subscriptions -----------------------------------------------------
    def get_subscription(self, subscription_id: str) -> Subscription:
        try:
            return self.subscriptions[subscription_id]
        except KeyError:
            raise NotFound("subscription", subscription_id) from None

    def list_subscriptions(self, customer_id: str) -> list[Subscription]:
        self.get_customer(customer_id)
        return sorted(
            (s for s in self.subscriptions.values() if s.customer_id == customer_id),
            key=lambda s: s.id,
        )

    def change_plan(
        self, subscription_id: str, *, plan: str, seats: int | None, effective: str
    ) -> Subscription:
        subscription = self.get_subscription(subscription_id)
        if subscription.status == "canceled":
            raise Conflict(
                "subscription_canceled",
                f"Subscription {subscription_id} is canceled and cannot change plan.",
            )
        if plan == subscription.plan and (seats is None or seats == subscription.seats):
            raise Unprocessable(
                "no_change_requested",
                f"Subscription {subscription_id} is already on {plan} "
                f"with {subscription.seats} seats.",
            )
        new_seats = seats if seats is not None else subscription.seats
        if plan == "enterprise" and new_seats < 10:
            raise Unprocessable(
                "enterprise_seat_minimum",
                "The enterprise plan requires at least 10 seats.",
            )
        updated = subscription.model_copy(
            update={
                "plan": plan,
                "seats": new_seats,
                "mrr_cents": _mrr_for(plan, new_seats),
                "renews_at": subscription.renews_at
                if effective == "next_cycle"
                else SEED_NOW + _days(30),
            }
        )
        self.subscriptions[subscription_id] = updated
        return updated

    def cancel_subscription(self, subscription_id: str, *, cancel_at: str) -> Subscription:
        subscription = self.get_subscription(subscription_id)
        if subscription.status == "canceled":
            raise Conflict(
                "subscription_already_canceled",
                f"Subscription {subscription_id} was already canceled.",
            )
        updated = subscription.model_copy(
            update={
                "status": "canceled",
                "canceled_at": SEED_NOW,
                "renews_at": None if cancel_at == "immediate" else subscription.renews_at,
            }
        )
        self.subscriptions[subscription_id] = updated
        return updated

    # -- invoices ----------------------------------------------------------
    def get_invoice(self, invoice_id: str) -> Invoice:
        try:
            return self.invoices[invoice_id]
        except KeyError:
            raise NotFound("invoice", invoice_id) from None

    def list_invoices(
        self, *, customer_id: str | None = None, status: str | None = None
    ) -> list[Invoice]:
        results = list(self.invoices.values())
        if customer_id:
            self.get_customer(customer_id)
            results = [i for i in results if i.customer_id == customer_id]
        if status:
            results = [i for i in results if i.status == status]
        # Newest first: "the last invoice" should be results[0].
        return sorted(results, key=lambda i: (i.issued_at, i.id), reverse=True)

    # -- tickets -----------------------------------------------------------
    def get_ticket(self, ticket_id: str) -> Ticket:
        try:
            return self.tickets[ticket_id]
        except KeyError:
            raise NotFound("ticket", ticket_id) from None

    def list_tickets(
        self, *, customer_id: str | None = None, status: str | None = None
    ) -> list[Ticket]:
        results = list(self.tickets.values())
        if customer_id:
            self.get_customer(customer_id)
            results = [t for t in results if t.customer_id == customer_id]
        if status:
            results = [t for t in results if t.status == status]
        return sorted(results, key=lambda t: t.id)

    def create_ticket(self, payload: dict[str, Any]) -> Ticket:
        self.get_customer(payload["customer_id"])
        ticket = Ticket(
            id=self._next_id("ticket", "TIC"),
            customer_id=payload["customer_id"],
            subject=payload["subject"],
            body=payload["body"],
            status="open",
            priority=payload.get("priority", "normal"),
            assignee=payload.get("assignee"),
            created_at=SEED_NOW,
            updated_at=SEED_NOW,
            comments=[],
        )
        self.tickets[ticket.id] = ticket
        return ticket

    def update_ticket(self, ticket_id: str, changes: dict[str, Any]) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        applied = {k: v for k, v in changes.items() if v is not None}
        if not applied:
            raise Unprocessable("empty_update", "Provide at least one field to change.")
        if ticket.status == "closed" and applied.get("status") != "open":
            raise Conflict(
                "ticket_closed",
                f"Ticket {ticket_id} is closed. Reopen it before making other changes.",
            )
        updated = ticket.model_copy(update={**applied, "updated_at": SEED_NOW})
        self.tickets[ticket_id] = updated
        return updated

    def add_comment(self, ticket_id: str, payload: dict[str, Any]) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        if ticket.status == "closed":
            raise Conflict("ticket_closed", f"Ticket {ticket_id} is closed and cannot take notes.")
        comment = TicketComment(
            id=self._next_id("comment", "CMT"),
            author=payload["author"],
            body=payload["body"],
            internal=payload.get("internal", False),
            created_at=SEED_NOW,
        )
        updated = ticket.model_copy(
            update={"comments": [*ticket.comments, comment], "updated_at": SEED_NOW}
        )
        self.tickets[ticket_id] = updated
        return updated

    # -- refunds -----------------------------------------------------------
    def list_refunds(self, *, invoice_id: str | None = None) -> list[Refund]:
        results = list(self.refunds.values())
        if invoice_id:
            results = [r for r in results if r.invoice_id == invoice_id]
        return sorted(results, key=lambda r: r.id)

    def create_refund(
        self, *, invoice_id: str, amount_cents: int, reason: str, actor: str
    ) -> Refund:
        """Issue a refund against an invoice.

        Four rules, and every one of them is a golden test case:
        the invoice must exist, it must have been paid, the amount must be
        positive, and it must not exceed the unrefunded balance.
        """
        invoice = self.get_invoice(invoice_id)

        if invoice.status in ("open", "void"):
            raise Unprocessable(
                "invoice_not_refundable",
                f"Invoice {invoice_id} has status {invoice.status!r}; "
                "only paid invoices can be refunded.",
            )
        if invoice.refundable_cents == 0:
            raise Unprocessable(
                "invoice_fully_refunded",
                f"Invoice {invoice_id} has already been fully refunded.",
            )
        if amount_cents > invoice.refundable_cents:
            raise Unprocessable(
                "refund_exceeds_balance",
                f"Cannot refund {_usd(amount_cents)} against invoice {invoice_id}: "
                f"only {_usd(invoice.refundable_cents)} remains unrefunded.",
            )

        refunded_total = invoice.refunded_cents + amount_cents
        self.invoices[invoice_id] = invoice.model_copy(
            update={
                "refunded_cents": refunded_total,
                "status": "refunded"
                if refunded_total == invoice.amount_cents
                else "partially_refunded",
            }
        )
        refund = Refund(
            id=self._next_id("refund", "REF"),
            invoice_id=invoice_id,
            customer_id=invoice.customer_id,
            amount_cents=amount_cents,
            reason=reason,
            status="succeeded",
            created_at=SEED_NOW,
            created_by=actor,
        )
        self.refunds[refund.id] = refund
        return refund


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_PLAN_PRICE_CENTS: dict[str, int] = {
    "free": 0,
    "starter": 2_900,
    "pro": 9_900,
    "enterprise": 24_900,
}


def _mrr_for(plan: str, seats: int) -> int:
    return _PLAN_PRICE_CENTS[plan] * seats


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------
def _seed_customers() -> list[Customer]:
    # Note CUS-1001 and CUS-1007 are both named "Ana Ruiz". That collision is
    # deliberate: it is what makes "refund Ana's invoice" ambiguous, and the
    # assistant must ask rather than pick one.
    rows = [
        ("CUS-1001", "Ana Ruiz", "ana@acme.io", "Acme Corp", "US", "active", 420),
        ("CUS-1002", "Dana Whitfield", "dana@northwind.co", "Northwind", "GB", "active", 380),
        ("CUS-1003", "Ravi Patel", "ravi@globex.dev", "Globex", "IN", "active", 310),
        ("CUS-1004", "Mei Lin", "mei@initech.io", "Initech", "SG", "trial", 12),
        ("CUS-1005", "Tomas Ferreira", "tomas@umbrella.co", "Umbrella", "PT", "active", 265),
        ("CUS-1006", "Sara Nowak", "sara@hooli.dev", "Hooli", "PL", "churned", 540),
        ("CUS-1007", "Ana Ruiz", "ana.ruiz@stark.io", "Stark Industries", "US", "active", 95),
        ("CUS-1008", "Jonas Berg", "jonas@vandelay.co", "Vandelay", "SE", "active", 150),
    ]
    return [
        Customer(
            id=cid,
            name=name,
            email=email,
            company=company,
            country=country,
            status=status,  # type: ignore[arg-type]
            created_at=SEED_NOW - _days(age),
        )
        for cid, name, email, company, country, status, age in rows
    ]


def _seed_subscriptions() -> list[Subscription]:
    rows = [
        ("SUB-2001", "CUS-1001", "pro", "active", 12, 400, 18),
        ("SUB-2002", "CUS-1002", "enterprise", "active", 40, 370, 5),
        ("SUB-2003", "CUS-1003", "starter", "past_due", 3, 300, 2),
        ("SUB-2004", "CUS-1004", "free", "active", 1, 12, 25),
        ("SUB-2005", "CUS-1005", "pro", "active", 8, 260, 11),
        ("SUB-2006", "CUS-1006", "starter", "canceled", 2, 530, None),
        ("SUB-2007", "CUS-1007", "starter", "active", 4, 90, 27),
        ("SUB-2008", "CUS-1008", "pro", "active", 6, 140, 9),
    ]
    return [
        Subscription(
            id=sid,
            customer_id=cid,
            plan=plan,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            seats=seats,
            mrr_cents=_mrr_for(plan, seats),
            started_at=SEED_NOW - _days(age),
            renews_at=None if renews is None else SEED_NOW + _days(renews),
            canceled_at=SEED_NOW - _days(30) if status == "canceled" else None,
        )
        for sid, cid, plan, status, seats, age, renews in rows
    ]


def _seed_invoices() -> list[Invoice]:
    # (id, customer, subscription, cents, status, issued days ago)
    rows = [
        ("INV-1001", "CUS-1001", "SUB-2001", 118_800, "paid", 120),
        ("INV-1002", "CUS-1002", "SUB-2002", 996_000, "paid", 95),
        ("INV-1003", "CUS-1003", "SUB-2003", 8_700, "open", 40),
        ("INV-1004", "CUS-1005", "SUB-2005", 79_200, "paid", 62),
        ("INV-1005", "CUS-1006", "SUB-2006", 5_800, "void", 58),
        ("INV-1006", "CUS-1002", "SUB-2002", 996_000, "open", 30),
        ("INV-1007", "CUS-1001", "SUB-2001", 24_000, "paid", 21),
        ("INV-1008", "CUS-1007", "SUB-2007", 11_600, "paid", 19),
        ("INV-1009", "CUS-1008", "SUB-2008", 59_400, "paid", 15),
        ("INV-1010", "CUS-1003", "SUB-2003", 8_700, "open", 10),
        ("INV-1011", "CUS-1005", "SUB-2005", 79_200, "open", 7),
        ("INV-1012", "CUS-1001", "SUB-2001", 118_800, "open", 3),
    ]
    return [
        Invoice(
            id=iid,
            customer_id=cid,
            subscription_id=sid,
            amount_cents=cents,
            refunded_cents=0,
            status=status,  # type: ignore[arg-type]
            issued_at=SEED_NOW - _days(issued),
            due_at=SEED_NOW - _days(issued) + _days(14),
            paid_at=SEED_NOW - _days(issued) + _days(2) if status == "paid" else None,
        )
        for iid, cid, sid, cents, status, issued in rows
    ]


def _seed_tickets() -> list[Ticket]:
    rows = [
        (
            "TIC-3001",
            "CUS-1001",
            "Invoice looks wrong",
            "Charged twice this month?",
            "open",
            "high",
            6,
        ),
        (
            "TIC-3002",
            "CUS-1003",
            "Payment keeps failing",
            "Card declined three times.",
            "pending",
            "urgent",
            4,
        ),
        (
            "TIC-3003",
            "CUS-1005",
            "Add SSO to our plan",
            "Do you support Okta?",
            "resolved",
            "normal",
            2,
        ),
    ]
    return [
        Ticket(
            id=tid,
            customer_id=cid,
            subject=subject,
            body=body,
            status=status,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            assignee=None,
            created_at=SEED_NOW - _days(age),
            updated_at=SEED_NOW - _days(age),
            comments=[],
        )
        for tid, cid, subject, body, status, priority, age in rows
    ]


#: Process-wide store. FastAPI hands this out via ``Depends(get_store)`` so tests
#: can swap or reset it in one place.
store = Store()


def get_store() -> Store:
    return store
