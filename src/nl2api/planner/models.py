"""The plan schema — what the model is allowed to say.

Two decisions here differ from the original sketch, both for good reasons.

**Arguments are a flat list of typed strings, not three free-form dicts.**
Structured outputs require ``additionalProperties: false`` on every object, which
an open ``dict[str, Any]`` cannot express. Modelling arguments as
``list[Argument]`` — each with a name, a location and a *string* value — keeps
the whole schema constrainable, and pushes type coercion into the validator
where it belongs: the model says ``"24000"``, and Phase 3 coerces it to an
integer only because the endpoint's JSON Schema says that field is an integer.
Explicit and narrow, rather than whatever the model felt like emitting.

**``depends_on`` is computed, not declared.** A step's dependencies are exactly
the steps its ``$steps.…`` references point at. Asking the model to restate that
adds a field it can get wrong for no information gain, so :attr:`PlanStep.depends_on`
derives it instead.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: ``$steps.s1.data[0].id`` or ``$steps.s1.amount_cents`` — a value copied from
#: an earlier step's response rather than written by the model.
STEP_REFERENCE = re.compile(r"^\$steps\.(?P<step>s\d+)(?P<path>(?:\.[^.\[\]]+|\[\d+\])*)$")

StepId = Annotated[str, Field(pattern=r"^s\d+$", description="Step identifier: s1, s2, …")]

ArgumentLocation = Literal["path", "query", "body"]


class StepReference(NamedTuple):
    """A parsed ``$steps.…`` reference."""

    step_id: str
    path: str
    raw: str


def parse_reference(value: str) -> StepReference | None:
    """Parse a ``$steps.…`` reference, or return ``None`` for a literal value."""
    match = STEP_REFERENCE.match(value.strip())
    if match is None:
        return None
    return StepReference(
        step_id=match.group("step"),
        path=match.group("path").lstrip("."),
        raw=value.strip(),
    )


class Argument(BaseModel):
    """One value the model wants to send, and where it goes."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Parameter or body field name, exactly as the schema spells it.")
    location: ArgumentLocation = Field(
        description="'path' for a URL path parameter, 'query' for a query string "
        "parameter, 'body' for a JSON request-body field."
    )
    value: str = Field(
        description="The value as a string. Use $steps.<step_id>.<path> to copy a "
        "value from an earlier step's response, e.g. $steps.s1.data[0].id."
    )

    @property
    def reference(self) -> StepReference | None:
        return parse_reference(self.value)

    @property
    def is_reference(self) -> bool:
        return self.reference is not None


class PlanStep(BaseModel):
    """One API call the model proposes."""

    model_config = ConfigDict(extra="forbid")

    id: StepId
    operation_id: str = Field(
        description="Must be one of the operation_id values in the provided catalogue."
    )
    arguments: list[Argument] = Field(
        default_factory=list, description="Every path parameter, query parameter and body field."
    )
    reason: str = Field(description="Why this step is needed, in one sentence.")
    expected_result: str = Field(description="What this step should return, in one sentence.")

    # -- derived -----------------------------------------------------------
    @property
    def references(self) -> tuple[StepReference, ...]:
        return tuple(a.reference for a in self.arguments if a.reference is not None)

    @property
    def depends_on(self) -> tuple[str, ...]:
        """Steps this one needs, derived from its references."""
        return tuple(dict.fromkeys(ref.step_id for ref in self.references))

    def arguments_in(self, location: ArgumentLocation) -> dict[str, str]:
        return {a.name: a.value for a in self.arguments if a.location == location}

    @property
    def path_params(self) -> dict[str, str]:
        return self.arguments_in("path")

    @property
    def query_params(self) -> dict[str, str]:
        return self.arguments_in("query")

    @property
    def body(self) -> dict[str, str]:
        return self.arguments_in("body")


class Plan(BaseModel):
    """The model's complete proposal for one request.

    Exactly one of three outcomes: a list of steps to run, a question to ask, or
    a refusal. The validator enforces that, so downstream code never has to
    handle a plan that both asks a question and proposes a refund.
    """

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="One sentence restating what the user asked for.")
    steps: list[PlanStep] = Field(
        default_factory=list,
        description="The calls to make, in order. Empty if asking or refusing.",
    )
    clarifying_question: str | None = Field(
        default=None,
        description="Ask this instead of guessing when the request is ambiguous or "
        "is missing a value you cannot look up. Steps must be empty when set.",
    )
    refusal: str | None = Field(
        default=None,
        description="Explain here when the request should not be carried out at all. "
        "Steps must be empty when set.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything you inferred rather than being told. Surfaced to the "
        "approver so a wrong assumption is caught before execution.",
    )

    # -- validation --------------------------------------------------------
    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Plan:
        if self.clarifying_question and self.refusal:
            raise ValueError("A plan cannot both ask a question and refuse; pick one.")
        if (self.clarifying_question or self.refusal) and self.steps:
            raise ValueError(
                "A plan that asks a question or refuses must have no steps — "
                f"got {len(self.steps)}."
            )
        return self

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> Plan:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"Duplicate step id {step.id!r}.")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _references_point_backwards(self) -> Plan:
        """A step may only reference steps declared before it.

        Enforced here rather than in the executor so a cyclic or forward-looking
        plan is rejected before anything runs.
        """
        available: set[str] = set()
        for step in self.steps:
            for reference in step.references:
                if reference.step_id == step.id:
                    raise ValueError(f"Step {step.id!r} references itself via {reference.raw!r}.")
                if reference.step_id not in available:
                    raise ValueError(
                        f"Step {step.id!r} references {reference.step_id!r} via "
                        f"{reference.raw!r}, but that step does not come before it."
                    )
            available.add(step.id)
        return self

    # -- derived -----------------------------------------------------------
    @property
    def needs_clarification(self) -> bool:
        return self.clarifying_question is not None

    @property
    def is_refusal(self) -> bool:
        return self.refusal is not None

    @property
    def is_actionable(self) -> bool:
        return bool(self.steps)

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(step.operation_id for step in self.steps)

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @classmethod
    def asking(cls, intent: str, question: str) -> Plan:
        return cls(intent=intent, clarifying_question=question)

    @classmethod
    def refusing(cls, intent: str, reason: str) -> Plan:
        return cls(intent=intent, refusal=reason)
