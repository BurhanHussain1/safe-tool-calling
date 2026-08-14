"""Describe what a write would do, without doing it.

This is the text a human reads with their finger over the Approve button, so it
is written for a support lead, not for a log parser: what changes, from what to
what, whether it can be undone.

**Non-mutation is structural, not a convention.** Nothing in this module calls
anything but a :class:`StateReader`, whose entire interface is one read method.
There is no code path here that could write, and the test suite snapshots the
datastore either side of a preview to prove it.

Current state is optional. Without a reader you still get a summary, a
reversibility verdict and warnings — enough to approve or reject — because a
preview that fails when the API is briefly unreachable would push people toward
approving blind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nl2api.guardrails.validator import ResolvedCall
from nl2api.schema.parser import ToolSpec
from nl2api.schema.registry import ToolRegistry, UnknownOperation


class StateReader(Protocol):
    """Reads current state for a preview. Read-only by construction."""

    def read(self, resource: str, identifier: str) -> dict[str, Any] | None:
        """Fetch one record, or ``None`` if it does not exist."""
        ...


@dataclass(frozen=True, slots=True)
class DryRunPreview:
    """What one write step would do."""

    step_id: str
    operation_id: str
    summary: str
    reversible: bool
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def changes(self) -> tuple[tuple[str, Any, Any], ...]:
        """Fields that differ between ``before`` and ``after``."""
        if self.before is None or self.after is None:
            return ()
        return tuple(
            (key, self.before.get(key), self.after[key])
            for key in self.after
            if key in self.before and self.before[key] != self.after[key]
        )

    def render(self) -> str:
        """The preview as shown to an approver."""
        lines = [self.summary]
        for key, old, new in self.changes:
            lines.append(f"  {key}: {old} → {new}")
        lines.append("  This can be undone." if self.reversible else "  This cannot be undone.")
        lines.extend(f"  ⚠ {w}" for w in self.warnings)
        return "\n".join(lines)


def build_previews(
    calls: tuple[ResolvedCall, ...],
    registry: ToolRegistry,
    reader: StateReader | None = None,
) -> tuple[DryRunPreview, ...]:
    """Preview every write in a validated plan. Reads are skipped."""
    previews: list[DryRunPreview] = []
    for call in calls:
        try:
            spec = registry.get(call.operation_id)
        except UnknownOperation:
            continue
        if not spec.is_write:
            continue
        previews.append(_preview(call, spec, reader))
    return tuple(previews)


def _preview(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> DryRunPreview:
    builder = _BUILDERS.get(call.operation_id, _generic)
    summary, before, after, warnings = builder(call, spec, reader)

    if call.has_deferred_values:
        # Say so rather than printing a raw $steps reference at an approver.
        deferred = ", ".join(sorted({d.name for d in call.deferred}))
        warnings = (
            *warnings,
            f"Some values ({deferred}) come from an earlier step and are not "
            "known yet. They are validated again before this step runs.",
        )

    return DryRunPreview(
        step_id=call.step_id,
        operation_id=call.operation_id,
        summary=summary,
        reversible=_REVERSIBLE.get(call.operation_id, not spec.needs_approval),
        before=before,
        after=after,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Per-operation builders
# ---------------------------------------------------------------------------
PreviewParts = tuple[str, dict[str, Any] | None, dict[str, Any] | None, tuple[str, ...]]
Builder = Callable[[ResolvedCall, ToolSpec, "StateReader | None"], PreviewParts]

#: Operations whose effect cannot be undone through this API. Everything else
#: falls back to "reversible unless it needs approval", which errs toward
#: warning rather than reassuring.
_REVERSIBLE: dict[str, bool] = {
    "create_refund": False,
    "delete_customer": False,
    "cancel_subscription": False,
    "change_subscription_plan": True,
    "create_ticket": True,
    "update_ticket": True,
    "add_ticket_comment": False,  # a customer-visible comment cannot be unsent
    "update_customer": True,
}


def _refund(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    raw_invoice = _field(call, "body", "invoice_id")
    amount = _field(call, "body", "amount_cents")
    invoice_id = _display(raw_invoice)
    warnings: list[str] = []

    invoice = _read(reader, "invoice", raw_invoice)
    before = after = None

    if invoice is not None and isinstance(amount, int):
        already = int(invoice.get("refunded_cents", 0))
        total = int(invoice.get("amount_cents", 0))
        refunded_after = already + amount
        before = {
            "status": invoice.get("status"),
            "refunded_cents": already,
            "refundable_cents": max(total - already, 0),
        }
        after = {
            "status": "refunded" if refunded_after >= total else "partially_refunded",
            "refunded_cents": refunded_after,
            "refundable_cents": max(total - refunded_after, 0),
        }
        if refunded_after > total:
            warnings.append(
                f"This would refund {_usd(refunded_after)} against a "
                f"{_usd(total)} invoice. The API will reject it."
            )

    summary = (
        f"Refund {_money(amount)} against invoice {invoice_id}"
        f"{_owner(invoice)}. Money moves back to the customer."
    )
    warnings.append("Repeating this call would issue a second refund.")
    return summary, before, after, tuple(warnings)


def _cancel(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    subscription_id = _field(call, "path", "subscription_id")
    when = _field(call, "body", "cancel_at") or "period_end"
    subscription = _read(reader, "subscription", subscription_id)

    before = after = None
    if subscription is not None:
        before = {"status": subscription.get("status"), "mrr_cents": subscription.get("mrr_cents")}
        after = {"status": "canceled", "mrr_cents": subscription.get("mrr_cents")}

    phrasing = "immediately" if when == "immediate" else "at the end of the billing period"
    summary = (
        f"Cancel subscription {_display(subscription_id)}{_owner(subscription)} "
        f"{phrasing}. Billing stops and paid service ends."
    )
    warnings = ("Reinstating requires creating a new subscription, not undoing this one.",)
    return summary, before, after, warnings


def _change_plan(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    subscription_id = _field(call, "path", "subscription_id")
    plan = _field(call, "body", "plan")
    seats = _field(call, "body", "seats")
    subscription = _read(reader, "subscription", subscription_id)

    before = after = None
    warnings: list[str] = []
    if subscription is not None:
        before = {
            "plan": subscription.get("plan"),
            "seats": subscription.get("seats"),
            "mrr_cents": subscription.get("mrr_cents"),
        }
        after = {
            "plan": subscription.get("plan") if plan in (None, DEFERRED) else plan,
            "seats": subscription.get("seats") if seats in (None, DEFERRED) else seats,
            "mrr_cents": None,
        }
        if before["mrr_cents"] is not None:
            warnings.append("Monthly revenue will be recalculated by the API.")

    when = _field(call, "body", "effective") or "next_cycle"
    phrasing = "immediately" if when == "immediate" else "from the next billing cycle"
    summary = (
        f"Move subscription {_display(subscription_id)}{_owner(subscription)} to the "
        f"{_display(plan)} plan {phrasing}. The customer's bill changes."
    )
    return summary, before, after, tuple(warnings)


def _delete_customer(
    call: ResolvedCall, spec: ToolSpec, reader: StateReader | None
) -> PreviewParts:
    customer_id = _field(call, "path", "customer_id")
    customer = _read(reader, "customer", customer_id)

    before = after = None
    if customer is not None:
        before = {"exists": True, "name": customer.get("name"), "status": customer.get("status")}
        after = {"exists": False, "name": None, "status": None}

    summary = (
        f"Permanently delete customer {_display(customer_id)}"
        f"{_owner(customer)} and their entire subscription history."
    )
    warnings = (
        "There is no undo and no backup. The records cannot be recovered.",
        "The API will refuse this while an active subscription exists.",
    )
    return summary, before, after, warnings


def _create_ticket(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    customer_id = _field(call, "body", "customer_id")
    priority = _field(call, "body", "priority") or "normal"
    customer = _read(reader, "customer", customer_id)
    summary = (
        f"Open a {_display(priority)}-priority support ticket "
        f"for {_display(customer_id)}{_owner(customer)}: "
        f"{_display(_field(call, 'body', 'subject'))!r}."
    )
    return summary, None, None, ()


def _add_comment(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    ticket_id = _field(call, "path", "ticket_id")
    internal = _field(call, "body", "internal") or False
    audience = "an internal note" if internal else "a comment the customer will see"
    summary = f"Add {audience} to ticket {_display(ticket_id)}."
    warnings = () if internal else ("A customer-visible comment cannot be unsent.",)
    return summary, None, None, warnings


def _generic(call: ResolvedCall, spec: ToolSpec, reader: StateReader | None) -> PreviewParts:
    """Fallback for any write without a hand-written preview.

    Uses the endpoint's declared ``x-side-effects``, which is why that field is
    required to be a sentence: it becomes approver-facing text the moment
    someone adds an endpoint and forgets to add a builder here.
    """
    target = next(iter(call.path_params.values()), None)
    subject = f" on {_display(target)}" if target else ""
    summary = f"{spec.summary}{subject}. {spec.side_effects}"
    return summary, None, None, ()


_BUILDERS: dict[str, Builder] = {
    "create_refund": _refund,
    "cancel_subscription": _cancel,
    "change_subscription_plan": _change_plan,
    "delete_customer": _delete_customer,
    "create_ticket": _create_ticket,
    "add_ticket_comment": _add_comment,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _Deferred:
    """Sentinel: this field is supplied by an earlier step.

    Distinct from ``None``, which means the field was genuinely not provided.
    Collapsing the two would show an approver "(not specified)" for a value that
    is in fact carefully wired up — the sort of misleading preview that gets
    someone to reject a correct plan, or wave through a wrong one.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<deferred>"


DEFERRED = _Deferred()


def _field(call: ResolvedCall, location: str, name: str) -> Any:
    """One argument's value: the literal, :data:`DEFERRED`, or ``None``."""
    if any(d.location == location and d.name == name for d in call.deferred):
        return DEFERRED
    source: dict[str, Any] = {
        "path": call.path_params,
        "query": call.query_params,
        "body": call.body or {},
    }[location]
    return source.get(name)


def _read(reader: StateReader | None, resource: str, identifier: Any) -> dict[str, Any] | None:
    """Read a record, tolerating an absent reader and an unresolved identifier."""
    if reader is None or not isinstance(identifier, str) or identifier.startswith("$steps."):
        return None
    return reader.read(resource, identifier)


def _display(value: Any) -> str:
    """Render a value for a human, marking anything not yet known."""
    if value is DEFERRED:
        return "(from an earlier step)"
    if value is None:
        return "(not specified)"
    text = str(value)
    if text.startswith("$steps."):
        return "(from an earlier step)"
    return text


def _owner(record: dict[str, Any] | None) -> str:
    """A parenthetical naming the customer, when we managed to read one."""
    if not record:
        return ""
    name = record.get("name")
    customer_id = record.get("customer_id") or record.get("id")
    if name:
        return f" ({name})"
    return f" ({customer_id})" if customer_id else ""


def _money(cents: Any) -> str:
    return (
        _usd(cents) if isinstance(cents, int) and not isinstance(cents, bool) else _display(cents)
    )


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
