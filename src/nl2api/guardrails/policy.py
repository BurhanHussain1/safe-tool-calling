"""Decide what happens to each step: run it, notice it, gate it, or refuse it.

The rules are deliberately few and ordered, so that "why was this blocked?" has
exactly one answer:

1. **Role.** The caller's role must satisfy the operation's minimum. Fails first,
   because an unauthorised call should never be evaluated on any other basis.
2. **Risk.** Read-only runs; a low-risk write runs but is reported; a high-risk
   write is held for human approval.
3. **Value ceiling.** A refund at or above the configured maximum is refused
   outright rather than gated — see :attr:`Settings.refund_max_cents`.
4. **Blast radius.** A plan touching more write endpoints than the configured
   cap is refused as a whole.

Rules 3 and 4 are the ones a policy engine actually earns its keep on. Risk and
role are properties of a single endpoint and could live on the endpoint; a
value ceiling and a blast-radius cap are properties of the *plan*, and nothing
else in the system is positioned to see them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from nl2api.config import Role, Settings, get_settings
from nl2api.guardrails.validator import ResolvedCall
from nl2api.mock_api.risk import RiskLevel
from nl2api.schema.registry import ToolRegistry, UnknownOperation


class Decision(IntEnum):
    """What may happen to a step. Ordered from most to least permissive.

    An ``IntEnum`` so the plan-level outcome is simply the maximum of its
    steps' — the strictest decision always wins, without a lookup table that
    could disagree with itself.
    """

    ALLOW = 0
    ALLOW_WITH_NOTICE = 1
    REQUIRE_APPROVAL = 2
    DENY = 3

    @property
    def blocks_execution(self) -> bool:
        return self in (Decision.REQUIRE_APPROVAL, Decision.DENY)

    @property
    def label(self) -> str:
        return {
            Decision.ALLOW: "allow",
            Decision.ALLOW_WITH_NOTICE: "allow and report",
            Decision.REQUIRE_APPROVAL: "hold for approval",
            Decision.DENY: "refuse",
        }[self]


_RISK_DECISIONS: dict[RiskLevel, Decision] = {
    RiskLevel.READ_ONLY: Decision.ALLOW,
    RiskLevel.LOW_RISK_WRITE: Decision.ALLOW_WITH_NOTICE,
    RiskLevel.HIGH_RISK_WRITE: Decision.REQUIRE_APPROVAL,
}


@dataclass(frozen=True, slots=True)
class StepDecision:
    """The verdict on one step, and every reason behind it."""

    step_id: str
    operation_id: str
    risk: RiskLevel
    decision: Decision
    reasons: tuple[str, ...] = ()

    @property
    def is_denied(self) -> bool:
        return self.decision is Decision.DENY

    @property
    def needs_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_APPROVAL


@dataclass(frozen=True, slots=True)
class PolicyReport:
    """The verdict on a whole plan."""

    decisions: tuple[StepDecision, ...] = ()
    plan_reasons: tuple[str, ...] = ()

    @property
    def outcome(self) -> Decision:
        """The strictest decision across all steps."""
        return max((d.decision for d in self.decisions), default=Decision.ALLOW)

    @property
    def may_execute_now(self) -> bool:
        return self.outcome in (Decision.ALLOW, Decision.ALLOW_WITH_NOTICE)

    @property
    def denied(self) -> tuple[StepDecision, ...]:
        return tuple(d for d in self.decisions if d.is_denied)

    @property
    def awaiting_approval(self) -> tuple[StepDecision, ...]:
        return tuple(d for d in self.decisions if d.needs_approval)

    @property
    def reasons(self) -> tuple[str, ...]:
        step_reasons = tuple(r for d in self.decisions for r in d.reasons)
        return self.plan_reasons + step_reasons

    def decision_for(self, step_id: str) -> StepDecision | None:
        return next((d for d in self.decisions if d.step_id == step_id), None)


class PolicyEngine:
    """Applies the four rules above to a validated plan."""

    def __init__(self, registry: ToolRegistry, settings: Settings | None = None) -> None:
        self.registry = registry
        self.settings = settings or get_settings()

    def evaluate(self, calls: tuple[ResolvedCall, ...], role: Role) -> PolicyReport:
        """Judge every call in a validated plan.

        Takes :class:`ResolvedCall` rather than a raw plan on purpose: policy runs
        on *coerced, schema-checked* values, so a rule comparing a refund against
        a ceiling is comparing integers, not whatever string the model wrote.
        """
        plan_reasons: list[str] = []
        decisions = [self._evaluate_step(call, role) for call in calls]

        write_count = sum(
            1 for d in decisions if _RISK_DECISIONS.get(d.risk, Decision.DENY) != Decision.ALLOW
        )
        if write_count > self.settings.max_write_steps:
            reason = (
                f"This plan changes data in {write_count} steps; the limit is "
                f"{self.settings.max_write_steps}. Break it into smaller requests."
            )
            plan_reasons.append(reason)
            decisions = [_with_denial(d, reason) if d.risk.is_write else d for d in decisions]

        return PolicyReport(decisions=tuple(decisions), plan_reasons=tuple(plan_reasons))

    # -- one step ----------------------------------------------------------
    def _evaluate_step(self, call: ResolvedCall, role: Role) -> StepDecision:
        try:
            spec = self.registry.get(call.operation_id)
        except UnknownOperation as exc:
            # Should be unreachable — the validator rejects unknown operations —
            # but failing closed here costs nothing and removes a whole class of
            # "what if the two disagree" reasoning.
            return StepDecision(
                step_id=call.step_id,
                operation_id=call.operation_id,
                risk=RiskLevel.HIGH_RISK_WRITE,
                decision=Decision.DENY,
                reasons=(str(exc),),
            )

        reasons: list[str] = []

        if not spec.permits(role):
            return StepDecision(
                step_id=call.step_id,
                operation_id=spec.operation_id,
                risk=spec.risk,
                decision=Decision.DENY,
                reasons=(
                    f"{spec.operation_id} requires the {spec.minimum_role.value!r} role; "
                    f"caller has {role.value!r}.",
                ),
            )

        decision = _RISK_DECISIONS[spec.risk]
        if decision is Decision.REQUIRE_APPROVAL:
            reasons.append(f"{spec.operation_id} is a high-risk write: {spec.side_effects}")
        elif decision is Decision.ALLOW_WITH_NOTICE:
            reasons.append(f"{spec.operation_id} changes data: {spec.side_effects}")

        ceiling_reason = self._refund_ceiling_breach(call)
        if ceiling_reason is not None:
            return StepDecision(
                step_id=call.step_id,
                operation_id=spec.operation_id,
                risk=spec.risk,
                decision=Decision.DENY,
                reasons=(*reasons, ceiling_reason),
            )

        return StepDecision(
            step_id=call.step_id,
            operation_id=spec.operation_id,
            risk=spec.risk,
            decision=decision,
            reasons=tuple(reasons),
        )

    def _refund_ceiling_breach(self, call: ResolvedCall) -> str | None:
        """Whether this call is a refund above the configured maximum.

        A deferred amount — one still carrying a ``$steps.…`` reference — cannot
        be checked here. It is checked again after resolution, before the call
        goes out, which is the only point at which the number actually exists.
        """
        if call.operation_id != "create_refund" or call.body is None:
            return None

        amount = call.body.get("amount_cents")
        if not isinstance(amount, int):
            return None

        ceiling = self.settings.refund_max_cents
        if amount < ceiling:
            return None
        return (
            f"Refund of {_usd(amount)} is at or above the {_usd(ceiling)} ceiling "
            "for assistant-issued refunds. A billing administrator must issue this "
            "one directly."
        )


def _with_denial(decision: StepDecision, reason: str) -> StepDecision:
    return StepDecision(
        step_id=decision.step_id,
        operation_id=decision.operation_id,
        risk=decision.risk,
        decision=Decision.DENY,
        reasons=(*decision.reasons, reason),
    )


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
