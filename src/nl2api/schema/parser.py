"""Parse an OpenAPI document into :class:`ToolSpec` objects.

Three things this module does that matter:

* **Inlines ``$ref``.** Each ToolSpec carries a self-contained JSON Schema, so
  Phase 3 can hand it straight to ``jsonschema`` with no external resolver and
  no chance of validating against a document that has since changed.
* **Fails closed on risk.** An operation with no ``x-risk`` is treated as
  ``high_risk_write`` requiring ``billing_admin``. Forgetting to declare risk
  makes an endpoint *harder* to call, never easier.
* **Drops header parameters.** ``X-Role`` and ``X-Actor`` are set by the
  executor from the caller's identity. They are not the model's to choose, so
  they never appear in the catalogue the model sees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from nl2api.config import Role
from nl2api.mock_api.risk import RiskLevel

logger = logging.getLogger(__name__)

ParameterLocation = Literal["path", "query"]

#: Parameter locations the model is allowed to fill in. Headers carry identity,
#: which the executor supplies; cookies are not used.
_PLANNABLE_LOCATIONS = {"path", "query"}

_SUCCESS_STATUS_CODES = ("200", "201", "202", "204")

#: Applied when an operation declares no risk. Deliberately the most
#: restrictive combination available.
_FAIL_CLOSED_RISK = RiskLevel.HIGH_RISK_WRITE
_FAIL_CLOSED_ROLE = Role.BILLING_ADMIN


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One path or query parameter."""

    name: str
    location: ParameterLocation
    required: bool
    schema: dict[str, Any]
    description: str = ""

    @property
    def json_type(self) -> str:
        """Best-effort type name for prompt rendering."""
        schema = self.schema
        if "type" in schema:
            return str(schema["type"])
        for key in ("anyOf", "oneOf"):
            options = [o.get("type") for o in schema.get(key, []) if o.get("type") != "null"]
            if options:
                return str(options[0])
        if "enum" in schema:
            return "string"
        return "any"

    @property
    def enum_values(self) -> tuple[str, ...]:
        """Legal values, if the parameter is constrained to a set."""
        if "enum" in self.schema:
            return tuple(str(v) for v in self.schema["enum"])
        for key in ("anyOf", "oneOf"):
            for option in self.schema.get(key, []):
                if "enum" in option:
                    return tuple(str(v) for v in option["enum"] if v is not None)
        return ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One callable operation, with everything needed to plan and police it."""

    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    parameters: tuple[ParameterSpec, ...]
    request_body_schema: dict[str, Any] | None
    request_body_required: bool
    response_schema: dict[str, Any] | None
    risk: RiskLevel
    required_roles: tuple[Role, ...]
    side_effects: str
    idempotent: bool
    tags: tuple[str, ...] = field(default=())

    # -- shape -------------------------------------------------------------
    @property
    def path_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.location == "path")

    @property
    def query_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.location == "query")

    @property
    def required_parameter_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters if p.required)

    def parameter(self, name: str) -> ParameterSpec | None:
        return next((p for p in self.parameters if p.name == name), None)

    @property
    def body_property_names(self) -> tuple[str, ...]:
        if not self.request_body_schema:
            return ()
        return tuple(self.request_body_schema.get("properties", {}))

    @property
    def required_body_property_names(self) -> tuple[str, ...]:
        if not self.request_body_schema:
            return ()
        return tuple(self.request_body_schema.get("required", []))

    # -- policy ------------------------------------------------------------
    @property
    def is_write(self) -> bool:
        return self.risk.is_write

    @property
    def needs_approval(self) -> bool:
        return self.risk.needs_approval

    @property
    def minimum_role(self) -> Role:
        """The least-privileged role permitted to call this operation."""
        return min(self.required_roles, key=lambda r: r.rank, default=_FAIL_CLOSED_ROLE)

    def permits(self, role: Role) -> bool:
        return role.implies(self.minimum_role)

    # -- rendering ---------------------------------------------------------
    @property
    def signature(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def search_text(self) -> str:
        """The text the retriever indexes.

        The operation id is split on underscores so ``create_refund`` matches a
        query containing "refund", and the path contributes its own vocabulary.
        """
        words = self.operation_id.replace("_", " ")
        path_words = self.path.replace("/", " ").replace("-", " ").replace("_", " ")
        params = " ".join(p.name.replace("_", " ") for p in self.parameters)
        body = " ".join(n.replace("_", " ") for n in self.body_property_names)
        return " ".join([words, self.summary, self.description, path_words, params, body])


class OpenAPIParseError(ValueError):
    """The document is not shaped the way an OpenAPI document should be."""


def parse_openapi(document: dict[str, Any]) -> list[ToolSpec]:
    """Extract every operation in ``document`` as a :class:`ToolSpec`.

    Operations without an ``operationId`` are skipped with a warning: the
    planner addresses endpoints by that identifier, so an operation without one
    is unreachable by design rather than by accident.
    """
    if "paths" not in document:
        raise OpenAPIParseError("Document has no 'paths' section; is this an OpenAPI document?")

    components = document.get("components", {}).get("schemas", {})
    specs: list[ToolSpec] = []

    for path, path_item in document["paths"].items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            spec = _parse_operation(
                path=path,
                method=method.upper(),
                operation=operation,
                shared_parameters=shared_params,
                components=components,
            )
            if spec is not None:
                specs.append(spec)

    return sorted(specs, key=lambda s: s.operation_id)


def _parse_operation(
    *,
    path: str,
    method: str,
    operation: dict[str, Any],
    shared_parameters: list[dict[str, Any]],
    components: dict[str, Any],
) -> ToolSpec | None:
    operation_id = operation.get("operationId")
    if not operation_id:
        logger.warning("Skipping %s %s: no operationId, so it cannot be planned.", method, path)
        return None

    parameters = tuple(
        _parse_parameter(raw, components)
        for raw in [*shared_parameters, *operation.get("parameters", [])]
        if raw.get("in") in _PLANNABLE_LOCATIONS
    )

    body_schema, body_required = _parse_request_body(operation, components)
    risk, roles = _parse_risk(operation, operation_id)

    return ToolSpec(
        operation_id=operation_id,
        method=method,
        path=path,
        summary=operation.get("summary", "").strip(),
        description=_clean_description(operation.get("description", "")),
        parameters=parameters,
        request_body_schema=body_schema,
        request_body_required=body_required,
        response_schema=_parse_response(operation, components),
        risk=risk,
        required_roles=roles,
        side_effects=operation.get("x-side-effects", "").strip(),
        idempotent=bool(operation.get("x-idempotent", False)),
        tags=tuple(operation.get("tags", [])),
    )


def _parse_parameter(raw: dict[str, Any], components: dict[str, Any]) -> ParameterSpec:
    return ParameterSpec(
        name=raw["name"],
        location=raw["in"],
        required=bool(raw.get("required", raw["in"] == "path")),
        schema=resolve_refs(raw.get("schema", {}), components),
        description=_clean_description(raw.get("description", "")),
    )


def _parse_request_body(
    operation: dict[str, Any], components: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    body = operation.get("requestBody")
    if not body:
        return None, False
    schema = body.get("content", {}).get("application/json", {}).get("schema")
    if schema is None:
        return None, False
    return resolve_refs(schema, components), bool(body.get("required", False))


def _parse_response(operation: dict[str, Any], components: dict[str, Any]) -> dict[str, Any] | None:
    responses = operation.get("responses", {})
    for code in _SUCCESS_STATUS_CODES:
        response = responses.get(code)
        if not response:
            continue
        schema = response.get("content", {}).get("application/json", {}).get("schema")
        if schema is not None:
            return resolve_refs(schema, components)
    return None


def _parse_risk(operation: dict[str, Any], operation_id: str) -> tuple[RiskLevel, tuple[Role, ...]]:
    """Read declared risk, failing closed when it is missing or unrecognised."""
    raw_risk = operation.get("x-risk")
    try:
        risk = RiskLevel(raw_risk)
    except ValueError:
        logger.warning(
            "Operation %r declares no usable x-risk (%r); treating it as %s.",
            operation_id,
            raw_risk,
            _FAIL_CLOSED_RISK.value,
        )
        return _FAIL_CLOSED_RISK, (_FAIL_CLOSED_ROLE,)

    roles: list[Role] = []
    for name in operation.get("x-required-roles", []):
        try:
            roles.append(Role(name))
        except ValueError:
            logger.warning("Operation %r lists unknown role %r; ignoring it.", operation_id, name)

    if not roles:
        logger.warning(
            "Operation %r declares no usable roles; restricting it to %s.",
            operation_id,
            _FAIL_CLOSED_ROLE.value,
        )
        return risk, (_FAIL_CLOSED_ROLE,)

    return risk, tuple(sorted(roles, key=lambda r: r.rank))


def resolve_refs(node: Any, components: dict[str, Any], _seen: frozenset[str] = frozenset()) -> Any:
    """Recursively inline ``$ref`` pointers into ``components``.

    A reference already on the current resolution stack is replaced with a
    permissive object rather than recursed into, so a self-referential schema
    cannot hang the parser. Sibling keys alongside a ``$ref`` (``description``,
    ``default``) override the target's, which matches OpenAPI 3.1 semantics.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name in _seen:
                return {"type": "object", "description": f"Recursive reference to {name}."}
            target = components.get(name)
            if target is None:
                logger.warning("Unresolvable $ref %r; substituting a permissive schema.", ref)
                return {}
            resolved = resolve_refs(target, components, _seen | {name})
            siblings = {
                k: resolve_refs(v, components, _seen) for k, v in node.items() if k != "$ref"
            }
            return {**resolved, **siblings}
        return {key: resolve_refs(value, components, _seen) for key, value in node.items()}

    if isinstance(node, list):
        return [resolve_refs(item, components, _seen) for item in node]

    return node


def _clean_description(text: str) -> str:
    """Collapse whitespace so descriptions render predictably in a prompt."""
    return " ".join(text.split())
