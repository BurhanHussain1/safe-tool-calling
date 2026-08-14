"""The assistant: one object that runs the whole pipeline in the right order.

    plan()     retrieve → plan → validate → policy → dry run → persist
    approve()  record a human decision
    execute()  resume an approved run

The ordering is the safety property. Validation runs before policy, policy
before previews, previews before anything is offered for approval — so an
invalid step is never policy-checked, and a forbidden step never reaches even a
read-only probe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nl2api.config import Role, Settings, get_settings
from nl2api.executor.client import ApiClient
from nl2api.executor.engine import ExecutionResult, WorkflowEngine
from nl2api.guardrails.dryrun import DryRunPreview, StateReader, build_previews
from nl2api.guardrails.policy import Decision, PolicyEngine, PolicyReport
from nl2api.guardrails.validator import PlanValidator, ResolvedCall, ValidationReport
from nl2api.persistence.repository import RunRepository, new_run_id
from nl2api.planner.planner import Planner, PlanningResult
from nl2api.schema.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AssistantResult:
    """Everything one turn produced, ready to render or return as JSON."""

    run_id: str
    request: str
    role: Role
    status: str
    planning: PlanningResult
    validation: ValidationReport
    policy: PolicyReport | None = None
    previews: tuple[DryRunPreview, ...] = ()
    execution: ExecutionResult | None = None
    message: str = ""
    clarifying_question: str | None = None
    pending_approval: tuple[str, ...] = field(default=())

    @property
    def needs_approval(self) -> bool:
        return self.status == "awaiting_approval"

    @property
    def calls(self) -> tuple[ResolvedCall, ...]:
        return self.validation.calls


class Assistant:
    """Turns a request into a safe, audited outcome."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        client: ApiClient,
        repository: RunRepository,
        planner: Planner | None = None,
        settings: Settings | None = None,
        reader: StateReader | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry
        self.client = client
        self.repository = repository
        self.reader = reader
        self.planner = planner or Planner(registry, settings=self.settings)
        self.validator = PlanValidator(registry)
        self.policy = PolicyEngine(registry, self.settings)
        self.engine = WorkflowEngine(registry, client, policy=self.policy, settings=self.settings)

    # -- plan --------------------------------------------------------------
    def plan(self, request: str, *, role: Role | None = None) -> AssistantResult:
        """Plan a request and take it as far as it can safely go."""
        role = role or self.settings.default_role
        run_id = new_run_id()

        planning = self.planner.plan(request, role=role)
        plan = planning.plan

        self.repository.create_run(
            run_id=run_id,
            request=request,
            role=role,
            status="planned",
            intent=plan.intent,
            candidates=planning.candidate_ids,
            clarifying_question=plan.clarifying_question,
            refusal=plan.refusal,
        )
        self.repository.log(run_id, "planned", {"operation_ids": list(plan.operation_ids)})

        if plan.is_refusal:
            return self._finish(
                run_id,
                request,
                role,
                planning,
                ValidationReport(),
                status="refused",
                message=plan.refusal or "",
            )

        if plan.needs_clarification:
            return self._finish(
                run_id,
                request,
                role,
                planning,
                ValidationReport(),
                status="needs_clarification",
                message=plan.clarifying_question or "",
                clarifying_question=plan.clarifying_question,
            )

        if planning.structural_errors:
            reason = " ".join(planning.structural_errors)
            self.repository.log(
                run_id, "structural_rejection", {"errors": list(planning.structural_errors)}
            )
            return self._finish(
                run_id,
                request,
                role,
                planning,
                ValidationReport(),
                status="rejected",
                message=reason,
            )

        validation = self.validator.validate(plan)
        if not validation.ok:
            self.repository.log(run_id, "validation_failed", {"errors": list(validation.messages)})
            self._persist_steps(run_id, planning, validation, None, ())
            return self._finish(
                run_id,
                request,
                role,
                planning,
                validation,
                status="rejected",
                message="The plan does not match the API's schema: "
                + "; ".join(validation.messages),
            )

        policy = self.policy.evaluate(validation.calls, role)
        previews = build_previews(validation.calls, self.registry, self.reader)
        self._persist_steps(run_id, planning, validation, policy, previews)

        if policy.outcome is Decision.DENY:
            self.repository.log(run_id, "policy_denied", {"reasons": list(policy.reasons)})
            return self._finish(
                run_id,
                request,
                role,
                planning,
                validation,
                status="blocked",
                policy=policy,
                previews=previews,
                message=" ".join(policy.reasons),
            )

        if policy.outcome is Decision.REQUIRE_APPROVAL:
            pending = tuple(d.step_id for d in policy.awaiting_approval)
            self.repository.log(run_id, "awaiting_approval", {"steps": list(pending)})
            self.repository.update_run(run_id, status="awaiting_approval")
            return self._finish(
                run_id,
                request,
                role,
                planning,
                validation,
                status="awaiting_approval",
                policy=policy,
                previews=previews,
                message="This plan needs approval before it can run.",
                pending_approval=pending,
            )

        return self._execute(run_id, request, role, planning, validation, policy, previews)

    # -- approve -----------------------------------------------------------
    def approve(
        self, run_id: str, step_id: str, *, approved: bool, decided_by: str, note: str | None = None
    ) -> None:
        self.repository.record_approval(
            run_id,
            step_id,
            decision="approved" if approved else "rejected",
            decided_by=decided_by,
            note=note,
        )
        self.repository.log(
            run_id,
            "approval_recorded",
            {"step_id": step_id, "approved": approved, "by": decided_by},
        )
        if not approved:
            self.repository.update_run(run_id, status="rejected", completed=True)

    # -- execute -----------------------------------------------------------
    def execute(self, run_id: str, request: str, role: Role) -> AssistantResult:
        """Re-plan, re-validate, then run with the approvals on record.

        Re-validating on resume is deliberate. An approval is a decision about a
        plan *and the state of the world it was previewed against*; replaying a
        stored plan blind would let something approved yesterday run against
        today's data.
        """
        result = self.plan(request, role=role)
        if result.status != "awaiting_approval":
            return result

        approved = self.repository.approved_steps(run_id)
        if not approved:
            return result

        return self._execute(
            run_id,
            request,
            role,
            result.planning,
            result.validation,
            result.policy,
            result.previews,
            approved=approved,
        )

    # -- internals ---------------------------------------------------------
    def _execute(
        self,
        run_id: str,
        request: str,
        role: Role,
        planning: PlanningResult,
        validation: ValidationReport,
        policy: PolicyReport | None,
        previews: tuple[DryRunPreview, ...],
        approved: frozenset[str] = frozenset(),
    ) -> AssistantResult:
        self.repository.update_run(run_id, status="executing")
        execution = self.engine.execute(
            validation.calls, role, run_id=run_id, approved_steps=approved
        )

        for result in execution.state.ordered:
            self.repository.record_outcome(
                run_id,
                result.step_id,
                status=result.status.value,
                status_code=result.status_code,
                response=result.response,
                error=result.error,
                latency_ms=result.latency_ms,
            )

        self.repository.update_run(
            run_id,
            status=execution.status,
            outcome=execution.status,
            final_answer=execution.message,
            clarifying_question=execution.clarifying_question,
            completed=execution.status in {"completed", "failed", "blocked", "halted"},
        )
        self.repository.log(run_id, f"run_{execution.status}", {"message": execution.message})

        return self._finish(
            run_id,
            request,
            role,
            planning,
            validation,
            status=execution.status,
            policy=policy,
            previews=previews,
            execution=execution,
            message=execution.message,
            clarifying_question=execution.clarifying_question,
            pending_approval=execution.pending_approval,
        )

    def _persist_steps(
        self,
        run_id: str,
        planning: PlanningResult,
        validation: ValidationReport,
        policy: PolicyReport | None,
        previews: tuple[DryRunPreview, ...],
    ) -> None:
        preview_by_step = {p.step_id: p for p in previews}
        for position, step in enumerate(planning.plan.steps):
            spec = (
                self.registry.get(step.operation_id) if step.operation_id in self.registry else None
            )
            decision = policy.decision_for(step.id) if policy else None
            preview = preview_by_step.get(step.id)
            self.repository.add_step(
                run_id,
                position=position,
                step_id=step.id,
                operation_id=step.operation_id,
                method=spec.method if spec else "?",
                path=spec.path if spec else "?",
                risk=spec.risk.value if spec else "unknown",
                arguments={a.name: a.value for a in step.arguments},
                validation={
                    "ok": not validation.errors_for(step.id),
                    "errors": [str(e) for e in validation.errors_for(step.id)],
                },
                decision=decision.decision.name if decision else None,
                decision_reasons=decision.reasons if decision else (),
                dry_run=_preview_payload(preview) if preview else None,
            )

    def _finish(
        self,
        run_id: str,
        request: str,
        role: Role,
        planning: PlanningResult,
        validation: ValidationReport,
        **kwargs: Any,
    ) -> AssistantResult:
        status = kwargs.pop("status")
        if status in {"refused", "needs_clarification", "rejected", "blocked"}:
            self.repository.update_run(run_id, status=status, completed=True)
        return AssistantResult(
            run_id=run_id,
            request=request,
            role=role,
            status=status,
            planning=planning,
            validation=validation,
            **kwargs,
        )


def _preview_payload(preview: DryRunPreview) -> dict[str, Any]:
    return {
        "summary": preview.summary,
        "reversible": preview.reversible,
        "before": preview.before,
        "after": preview.after,
        "warnings": list(preview.warnings),
        "rendered": preview.render(),
    }
