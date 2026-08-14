"""Validate a plan against the API's own JSON Schema.

Every value the model produces is a string (see :mod:`nl2api.planner.models`).
This module turns those strings into typed values and checks them, using two
rules that are easy to state and easy to test:

**Coercion is narrow.** ``"24000"`` becomes ``24000`` because the schema says
that field is an integer. ``"24,000"``, ``"$240"``, ``"24000.5"`` and ``"lots"``
are all errors. There is no "do what I mean" — a lenient parser is how a refund
of the wrong size gets issued.

**Unknown names are errors, not extras.** A parameter or body field the schema
does not declare is rejected outright, so the model cannot smuggle in a
``skip_approval`` field on the chance that something honours it.

Values carrying a ``$steps.…`` reference cannot be checked here, because they do
not exist yet. They are recorded as deferred and validated again in Phase 4 once
resolved — data coming back from an API response is no more trusted than data
coming from the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from nl2api.planner.models import Argument, Plan, PlanStep, StepReference
from nl2api.schema.parser import ToolSpec
from nl2api.schema.registry import ToolRegistry, UnknownOperation

_INTEGER = re.compile(r"^-?\d+$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0"})


@dataclass(frozen=True, slots=True)
class FieldError:
    """One specific thing wrong with one specific value."""

    step_id: str
    location: str
    name: str
    code: str
    message: str

    def __str__(self) -> str:
        where = f"{self.step_id}.{self.location}.{self.name}" if self.name else self.step_id
        return f"{where}: {self.message}"


@dataclass(frozen=True, slots=True)
class DeferredValue:
    """A value that will only be known once an earlier step has run."""

    location: str
    name: str
    reference: StepReference


@dataclass(frozen=True, slots=True)
class ResolvedCall:
    """A validated, typed call — everything the executor needs but the URL."""

    step_id: str
    operation_id: str
    method: str
    path_template: str
    path_params: dict[str, Any] = field(default_factory=dict)
    query_params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    deferred: tuple[DeferredValue, ...] = ()

    @property
    def has_deferred_values(self) -> bool:
        return bool(self.deferred)

    def url_path(self) -> str:
        """Substitute path parameters. Raises if any are still unresolved."""
        path = self.path_template
        for name, value in self.path_params.items():
            path = path.replace(f"{{{name}}}", str(value))
        if "{" in path:
            raise ValueError(f"Path {path!r} still has unresolved parameters.")
        return path


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The verdict on a whole plan."""

    calls: tuple[ResolvedCall, ...] = ()
    errors: tuple[FieldError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(str(e) for e in self.errors)

    def errors_for(self, step_id: str) -> tuple[FieldError, ...]:
        return tuple(e for e in self.errors if e.step_id == step_id)

    def call(self, step_id: str) -> ResolvedCall | None:
        return next((c for c in self.calls if c.step_id == step_id), None)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------
def target_type(schema: dict[str, Any]) -> str | None:
    """The concrete JSON type a schema expects, seeing through optionality.

    FastAPI renders ``str | None`` as ``anyOf: [{string}, {null}]``, so the real
    type is the single non-null branch. A genuine union of two value types
    returns ``None`` — we will not guess which one the model meant.
    """
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        concrete = [t for t in declared if t != "null"]
        return concrete[0] if len(concrete) == 1 else None

    for key in ("anyOf", "oneOf"):
        branches = [b for b in schema.get(key, []) if b.get("type") != "null"]
        if len(branches) == 1:
            return target_type(branches[0])
        if branches:
            return None

    if "enum" in schema or "const" in schema:
        return "string"
    return None


def coerce(value: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    """Convert a model-supplied string to the type the schema wants.

    Returns ``(coerced_value, error_message)``; exactly one is meaningful. The
    accepted forms are deliberately few — see the module docstring.
    """
    expected = target_type(schema)

    if expected in (None, "string"):
        return value, None

    text = value.strip()

    if expected == "integer":
        if not _INTEGER.match(text):
            return None, (
                f"expected a whole number, got {value!r} "
                "(digits only — no currency symbols, commas or decimals)"
            )
        return int(text), None

    if expected == "number":
        if not _NUMBER.match(text):
            return None, f"expected a number, got {value!r}"
        return float(text), None

    if expected == "boolean":
        lowered = text.lower()
        if lowered in _TRUE:
            return True, None
        if lowered in _FALSE:
            return False, None
        return None, f"expected true or false, got {value!r}"

    if expected == "null":
        if text.lower() in {"", "null", "none"}:
            return None, None
        return None, f"expected null, got {value!r}"

    if expected in ("array", "object"):
        # No endpoint in this API takes a structured parameter, and inventing a
        # splitting convention would be exactly the kind of lenient guessing
        # this module exists to avoid.
        return None, (
            f"cannot supply a {expected} as a plain string; this API has no "
            f"such parameter, so {value!r} is almost certainly a mistake"
        )

    return value, None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class PlanValidator:
    """Checks a plan against the tool catalogue it claims to target."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def validate(self, plan: Plan) -> ValidationReport:
        if not plan.is_actionable:
            # A question or a refusal has nothing to validate, and calling it
            # invalid would make "the model correctly declined" look like a bug.
            return ValidationReport()

        calls: list[ResolvedCall] = []
        errors: list[FieldError] = []

        for step in plan.steps:
            try:
                spec = self.registry.get(step.operation_id)
            except UnknownOperation as exc:
                errors.append(
                    FieldError(
                        step_id=step.id,
                        location="step",
                        name="operation_id",
                        code="unknown_operation",
                        message=str(exc),
                    )
                )
                continue

            call, step_errors = self._validate_step(step, spec)
            errors.extend(step_errors)
            if not step_errors:
                calls.append(call)

        return ValidationReport(calls=tuple(calls), errors=tuple(errors))

    # -- one step ----------------------------------------------------------
    def _validate_step(
        self, step: PlanStep, spec: ToolSpec
    ) -> tuple[ResolvedCall, list[FieldError]]:
        errors: list[FieldError] = []
        deferred: list[DeferredValue] = []
        coerced: dict[str, dict[str, Any]] = {"path": {}, "query": {}, "body": {}}

        for argument in step.arguments:
            error = self._validate_argument(step, spec, argument, coerced, deferred)
            if error is not None:
                errors.append(error)

        errors.extend(self._check_missing(step, spec, coerced, deferred))
        errors.extend(self._check_body_shape(step, spec, coerced["body"], deferred))

        call = ResolvedCall(
            step_id=step.id,
            operation_id=spec.operation_id,
            method=spec.method,
            path_template=spec.path,
            path_params=coerced["path"],
            query_params=coerced["query"],
            body=coerced["body"] if spec.request_body_schema else None,
            deferred=tuple(deferred),
        )
        return call, errors

    def _validate_argument(
        self,
        step: PlanStep,
        spec: ToolSpec,
        argument: Argument,
        coerced: dict[str, dict[str, Any]],
        deferred: list[DeferredValue],
    ) -> FieldError | None:
        schema = self._schema_for(spec, argument)
        if schema is None:
            return FieldError(
                step_id=step.id,
                location=argument.location,
                name=argument.name,
                code="unknown_field",
                message=(
                    f"{spec.operation_id} has no {argument.location} field named {argument.name!r}."
                ),
            )

        reference = argument.reference
        if reference is not None:
            # The value does not exist yet. Record it and re-validate in Phase 4.
            deferred.append(
                DeferredValue(location=argument.location, name=argument.name, reference=reference)
            )
            return None

        value, message = coerce(argument.value, schema)
        if message is not None:
            return FieldError(
                step_id=step.id,
                location=argument.location,
                name=argument.name,
                code="type_mismatch",
                message=f"{argument.name}: {message}",
            )

        constraint = schema_violation(value, schema)
        if constraint is not None:
            return FieldError(
                step_id=step.id,
                location=argument.location,
                name=argument.name,
                code="constraint_violation",
                message=f"{argument.name}: {constraint}",
            )

        coerced[argument.location][argument.name] = value
        return None

    @staticmethod
    def _schema_for(spec: ToolSpec, argument: Argument) -> dict[str, Any] | None:
        if argument.location in ("path", "query"):
            parameter = spec.parameter(argument.name)
            if parameter is None or parameter.location != argument.location:
                return None
            return parameter.schema
        body = spec.request_body_schema
        if not body:
            return None
        return body.get("properties", {}).get(argument.name)

    def _check_missing(
        self,
        step: PlanStep,
        spec: ToolSpec,
        coerced: dict[str, dict[str, Any]],
        deferred: list[DeferredValue],
    ) -> list[FieldError]:
        """Required parameters that were neither supplied nor deferred."""
        supplied = {(d.location, d.name) for d in deferred} | {
            (location, name) for location, values in coerced.items() for name in values
        }

        return [
            FieldError(
                step_id=step.id,
                location=parameter.location,
                name=parameter.name,
                code="missing_required",
                message=(
                    f"{spec.operation_id} requires the {parameter.location} parameter "
                    f"{parameter.name!r}."
                ),
            )
            for parameter in spec.parameters
            if parameter.required and (parameter.location, parameter.name) not in supplied
        ]

    def _check_body_shape(
        self,
        step: PlanStep,
        spec: ToolSpec,
        body: dict[str, Any],
        deferred: list[DeferredValue],
    ) -> list[FieldError]:
        errors: list[FieldError] = []
        deferred_body = {d.name for d in deferred if d.location == "body"}

        if body or deferred_body:
            if not spec.request_body_schema:
                errors.append(
                    FieldError(
                        step_id=step.id,
                        location="body",
                        name="",
                        code="unexpected_body",
                        message=f"{spec.operation_id} does not take a request body.",
                    )
                )
                return errors
        elif spec.request_body_required:
            errors.append(
                FieldError(
                    step_id=step.id,
                    location="body",
                    name="",
                    code="missing_body",
                    message=f"{spec.operation_id} requires a request body.",
                )
            )
            return errors

        errors.extend(
            FieldError(
                step_id=step.id,
                location="body",
                name=name,
                code="missing_required",
                message=f"{spec.operation_id} requires the body field {name!r}.",
            )
            for name in spec.required_body_property_names
            if name not in body and name not in deferred_body
        )

        # Validate the assembled body as a whole. Skipped when values are still
        # deferred: a partial body would fail its own required-fields check and
        # report an error that is not actually there.
        if not deferred_body and spec.request_body_schema and body:
            violation = schema_violation(body, spec.request_body_schema)
            if violation is not None:
                errors.append(
                    FieldError(
                        step_id=step.id,
                        location="body",
                        name="",
                        code="constraint_violation",
                        message=violation,
                    )
                )

        return errors


def schema_violation(value: Any, schema: dict[str, Any]) -> str | None:
    """The first JSON Schema error for ``value``, as a readable sentence.

    Public because the executor calls it again on resolved values: data from an
    API response gets the same scrutiny as data from the model.
    """
    validator = Draft202012Validator(schema)
    error = next(iter(validator.iter_errors(value)), None)
    if error is None:
        return None
    location = ".".join(str(p) for p in error.path)
    return f"{location}: {error.message}" if location else error.message
