"""Run a validated, approved plan — one step at a time, halting on doubt.

Four rules govern the loop:

**Nothing runs that policy did not clear.** A denied step, or a high-risk step
whose approval has not been given, blocks the run rather than executing.

**Resolved values are re-validated.** A value copied out of an API response is
no more trusted than one written by the model. After resolution the step is
re-checked against the schema *and* re-run through the policy engine, which is
the only point at which a deferred refund amount can be compared to the ceiling.

**Ambiguity halts.** A lookup that returns nothing cannot be referenced. A
lookup meant to identify one record that returns several is a question, not a
value — the run stops and asks. It never takes ``data[0]``.

**Failure stops the line.** A failed step leaves the rest ``skipped``, because a
later step written to consume its result would otherwise run on stale state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from nl2api.config import Role, Settings, get_settings
from nl2api.executor.client import ApiClient, TransportError
from nl2api.executor.resolver import ResolutionError, resolve
from nl2api.executor.state import StepResult, StepStatus, WorkflowState
from nl2api.guardrails.policy import Decision, PolicyEngine
from nl2api.guardrails.validator import ResolvedCall, schema_violation
from nl2api.schema.registry import ToolRegistry

logger = logging.getLogger(__name__)

RunStatus = Literal["completed", "failed", "halted", "awaiting_approval", "blocked"]

#: Operations whose job is to identify *one* record. More than one result is a
#: question, not a value.
#:
#: The distinction this encodes: resolving *who someone is* must be unambiguous,
#: while picking the first element of an ordered list is a legitimate selection —
#: "the last invoice" really is ``data[0]`` of invoices sorted newest-first. Only
#: the former is listed here. This is a judgement call about intent, which is why
#: it is a named constant rather than a rule inferred from the response shape.
IDENTITY_OPERATIONS: frozenset[str] = frozenset({"search_customers"})


@dataclass(slots=True)
class ExecutionResult:
    """What happened to a run."""

    state: WorkflowState
    status: RunStatus
    message: str = ""
    clarifying_question: str | None = None
    pending_approval: tuple[str, ...] = field(default=())

    @property
    def needs_clarification(self) -> bool:
        return self.clarifying_question is not None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class WorkflowEngine:
    """Executes the steps of an approved plan."""

    def __init__(
        self,
        registry: ToolRegistry,
        client: ApiClient,
        *,
        policy: PolicyEngine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry
        self.client = client
        self.settings = settings or get_settings()
        self.policy = policy or PolicyEngine(registry, self.settings)

    def execute(
        self,
        calls: tuple[ResolvedCall, ...],
        role: Role,
        *,
        run_id: str,
        approved_steps: frozenset[str] = frozenset(),
    ) -> ExecutionResult:
        """Run ``calls`` in order.

        Plan validation already guarantees references point backwards only, so
        the declared order is a valid topological order and no sort is needed.
        """
        state = WorkflowState(run_id=run_id, role=role)

        for index, call in enumerate(calls):
            outcome = self._run_step(call, role, state, approved_steps)
            if outcome is not None:
                _skip_remaining(calls[index + 1 :], state)
                return outcome

        return ExecutionResult(
            state=state, status="completed", message="All steps completed successfully."
        )

    # -- one step ----------------------------------------------------------
    def _run_step(
        self,
        call: ResolvedCall,
        role: Role,
        state: WorkflowState,
        approved_steps: frozenset[str],
    ) -> ExecutionResult | None:
        """Run one step. Returns a terminal result if the run should stop."""
        # 1. Ambiguity gate, before resolution rather than after.
        #    An empty lookup would otherwise surface as "index 0 is out of
        #    range" — technically true, useless to a person. The gate turns the
        #    same situation into a question they can answer.
        question = self._ambiguity(call, state)
        if question is not None:
            state.record(
                StepResult(
                    step_id=call.step_id,
                    operation_id=call.operation_id,
                    status=StepStatus.AMBIGUOUS,
                    error=question,
                )
            )
            return ExecutionResult(
                state=state,
                status="halted",
                message="Stopped to ask rather than guess.",
                clarifying_question=question,
            )

        # 2. Resolve values from earlier steps.
        try:
            resolved = self._resolve(call, state)
        except ResolutionError as exc:
            state.record(_blocked(call, str(exc)))
            return ExecutionResult(state=state, status="halted", message=str(exc))

        # 3. Re-check the now-concrete call. A value from an API response gets
        #    the same scrutiny as one from the model.
        violation = self._recheck_schema(resolved)
        if violation is not None:
            state.record(_blocked(resolved, violation))
            return ExecutionResult(state=state, status="halted", message=violation)

        decision = self.policy.evaluate((resolved,), role).decisions[0]
        if decision.decision is Decision.DENY:
            reason = " ".join(decision.reasons) or "Policy refused this step."
            state.record(_blocked(resolved, reason))
            return ExecutionResult(state=state, status="blocked", message=reason)

        if decision.decision is Decision.REQUIRE_APPROVAL and call.step_id not in approved_steps:
            state.record(_blocked(resolved, "Awaiting human approval."))
            return ExecutionResult(
                state=state,
                status="awaiting_approval",
                message=f"Step {call.step_id} needs approval before it can run.",
                pending_approval=(call.step_id,),
            )

        # 4. Send it.
        try:
            response = self.client.send(resolved, role)
        except TransportError as exc:
            state.record(
                StepResult(
                    step_id=call.step_id,
                    operation_id=call.operation_id,
                    status=StepStatus.FAILED,
                    error=f"Could not reach the API: {exc}",
                )
            )
            return ExecutionResult(
                state=state, status="failed", message=f"Could not reach the API: {exc}"
            )

        if not response.ok:
            message = response.error_message or f"HTTP {response.status_code}"
            state.record(
                StepResult(
                    step_id=call.step_id,
                    operation_id=call.operation_id,
                    status=StepStatus.FAILED,
                    status_code=response.status_code,
                    response=response.body,
                    error=message,
                    latency_ms=response.latency_ms,
                )
            )
            return ExecutionResult(
                state=state,
                status="failed",
                message=f"Step {call.step_id} failed — {message}",
            )

        state.record(
            StepResult(
                step_id=call.step_id,
                operation_id=call.operation_id,
                status=StepStatus.OK,
                status_code=response.status_code,
                response=response.body,
                latency_ms=response.latency_ms,
            )
        )
        return None

    # -- helpers -----------------------------------------------------------
    def _resolve(self, call: ResolvedCall, state: WorkflowState) -> ResolvedCall:
        """Return a copy of ``call`` with every reference replaced by its value."""
        if not call.has_deferred_values:
            return call

        path = dict(call.path_params)
        query = dict(call.query_params)
        body = dict(call.body) if call.body is not None else None

        for deferred in call.deferred:
            value = resolve(deferred.reference, state)
            target = {"path": path, "query": query, "body": body}[deferred.location]
            if target is None:
                raise ResolutionError(
                    f"{call.operation_id} takes no request body, "
                    f"so {deferred.name!r} cannot be set."
                )
            target[deferred.name] = value

        return ResolvedCall(
            step_id=call.step_id,
            operation_id=call.operation_id,
            method=call.method,
            path_template=call.path_template,
            path_params=path,
            query_params=query,
            body=body,
            deferred=(),
        )

    def _ambiguity(self, call: ResolvedCall, state: WorkflowState) -> str | None:
        """Whether a step this call depends on returned an unusable number of records."""
        for deferred in call.deferred:
            source = state.results.get(deferred.reference.step_id)
            if source is None or not source.ok:
                continue

            records = _record_list(source.response)
            if records is None:
                continue

            if not records:
                return (
                    f"I could not find anything matching that lookup "
                    f"(step {source.step_id}, {source.operation_id}). "
                    "Could you check the details and try again?"
                )

            if len(records) > 1 and source.operation_id in IDENTITY_OPERATIONS:
                return (
                    f"That matched {len(records)} records, so I stopped rather than "
                    f"guess which one you meant:\n{_candidates(records)}\n"
                    "Which one should I use?"
                )
        return None

    def _recheck_schema(self, call: ResolvedCall) -> str | None:
        """Validate resolved values against the endpoint's schema."""
        spec = self.registry.get(call.operation_id)

        for name, value in {**call.path_params, **call.query_params}.items():
            parameter = spec.parameter(name)
            if parameter is None:
                continue
            violation = schema_violation(value, parameter.schema)
            if violation is not None:
                return f"{call.step_id}.{name}: resolved value is invalid — {violation}"

        if call.body and spec.request_body_schema:
            violation = schema_violation(call.body, spec.request_body_schema)
            if violation is not None:
                return f"{call.step_id}.body: resolved body is invalid — {violation}"

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _record_list(response: Any) -> list[Any] | None:
    """The ``data`` array of a list response, or ``None`` for a single record."""
    if isinstance(response, dict) and isinstance(response.get("data"), list):
        return response["data"]
    return None


def _candidates(records: list[Any]) -> str:
    lines = []
    for record in records[:5]:
        if isinstance(record, dict):
            label = record.get("name") or record.get("subject") or record.get("id")
            detail = record.get("email") or record.get("company") or ""
            lines.append(f"  - {record.get('id')}: {label}{f' ({detail})' if detail else ''}")
        else:
            lines.append(f"  - {record}")
    if len(records) > 5:
        lines.append(f"  … and {len(records) - 5} more")
    return "\n".join(lines)


def _blocked(call: ResolvedCall, reason: str) -> StepResult:
    return StepResult(
        step_id=call.step_id,
        operation_id=call.operation_id,
        status=StepStatus.BLOCKED,
        error=reason,
    )


def _skip_remaining(calls: tuple[ResolvedCall, ...], state: WorkflowState) -> None:
    """Mark steps that never ran, so the audit log shows why they did not."""
    for call in calls:
        state.record(
            StepResult(
                step_id=call.step_id,
                operation_id=call.operation_id,
                status=StepStatus.SKIPPED,
                error="An earlier step ended the run.",
            )
        )
