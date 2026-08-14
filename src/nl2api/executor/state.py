"""Typed workflow state.

Values pass between steps through this object and nowhere else. There is no
"the model remembers what the customer id was" — a value used in step 3 was
either written by the user, validated from the model, or copied verbatim out of
a recorded response in :attr:`WorkflowState.results`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nl2api.config import Role


class StepStatus(StrEnum):
    """How one step ended."""

    OK = "ok"
    FAILED = "failed"
    #: Never attempted, because an earlier step ended the run.
    SKIPPED = "skipped"
    #: Policy refused it, or it needs an approval that has not been given.
    BLOCKED = "blocked"
    #: Halted because the result was ambiguous and guessing was not acceptable.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class StepResult:
    """The outcome of one step, including the response it produced."""

    step_id: str
    operation_id: str
    status: StepStatus
    status_code: int | None = None
    response: Any | None = None
    error: str | None = None
    latency_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is StepStatus.OK


@dataclass(slots=True)
class WorkflowState:
    """Everything one run has learned so far."""

    run_id: str
    role: Role
    results: dict[str, StepResult] = field(default_factory=dict)

    def record(self, result: StepResult) -> None:
        self.results[result.step_id] = result

    def response(self, step_id: str) -> Any | None:
        result = self.results.get(step_id)
        return result.response if result else None

    def has(self, step_id: str) -> bool:
        return step_id in self.results

    @property
    def ordered(self) -> tuple[StepResult, ...]:
        return tuple(self.results.values())

    @property
    def succeeded(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results.values())

    @property
    def writes_performed(self) -> tuple[str, ...]:
        """Steps that actually reached the API and changed something.

        The number the adversarial test suite asserts is zero.
        """
        return tuple(r.step_id for r in self.results.values() if r.ok and r.status_code != 200)
