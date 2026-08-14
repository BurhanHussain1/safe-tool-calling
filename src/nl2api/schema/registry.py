"""The tool catalogue: lookup by operation id, and rendering for a prompt.

Rendering is deliberately compact and deliberately complete. The model needs
enough to fill parameters correctly, and nothing that would let it think it can
decide policy — risk is shown so it can *plan* around an approval gate, never so
it can waive one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from nl2api.config import Role
from nl2api.schema.parser import ToolSpec, parse_openapi


class UnknownOperation(KeyError):
    """The plan referenced an operation that is not in the catalogue.

    Raised rather than returning ``None`` because every caller treats this as
    fatal for the step, and a silent ``None`` is how a hallucinated endpoint
    turns into a confusing downstream error instead of a clear rejection.
    """

    def __init__(self, operation_id: str, known: Iterable[str] = ()) -> None:
        self.operation_id = operation_id
        self.known = tuple(known)
        suggestion = _closest(operation_id, self.known)
        message = f"No operation named {operation_id!r}."
        if suggestion:
            message += f" Did you mean {suggestion!r}?"
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # KeyError repr-quotes its argument; this reads better.
        return self.message


class ToolRegistry:
    """An immutable collection of :class:`ToolSpec`, keyed by operation id."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.operation_id in self._specs:
                raise ValueError(f"Duplicate operation id {spec.operation_id!r} in catalogue.")
            self._specs[spec.operation_id] = spec

    @classmethod
    def from_openapi(cls, document: dict[str, Any]) -> ToolRegistry:
        return cls(parse_openapi(document))

    # -- access ------------------------------------------------------------
    def get(self, operation_id: str) -> ToolSpec:
        try:
            return self._specs[operation_id]
        except KeyError:
            raise UnknownOperation(operation_id, self._specs) from None

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def operation_ids(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def reads(self) -> list[ToolSpec]:
        return [s for s in self._specs.values() if not s.is_write]

    def writes(self) -> list[ToolSpec]:
        return [s for s in self._specs.values() if s.is_write]

    def requiring_approval(self) -> list[ToolSpec]:
        return [s for s in self._specs.values() if s.needs_approval]

    def callable_by(self, role: Role) -> list[ToolSpec]:
        """Everything ``role`` is permitted to call.

        Used to trim the catalogue before it reaches the model: an operation the
        caller could never perform should not be offered as an option.
        """
        return [s for s in self._specs.values() if s.permits(role)]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, operation_id: object) -> bool:
        return operation_id in self._specs

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    # -- rendering ---------------------------------------------------------
    def render_for_prompt(self, operation_ids: Iterable[str] | None = None) -> str:
        """Render a catalogue of the given operations as JSON for the prompt.

        JSON rather than prose because the model has to reproduce these names
        exactly, and prose invites paraphrase.
        """
        ids = list(operation_ids) if operation_ids is not None else list(self._specs)
        catalogue = [_render_tool(self.get(op_id)) for op_id in ids]
        return json.dumps(catalogue, indent=2, ensure_ascii=False)


def _render_tool(spec: ToolSpec) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "operation_id": spec.operation_id,
        "method": spec.method,
        "path": spec.path,
        "summary": spec.summary,
        "risk": spec.risk.value,
        "minimum_role": spec.minimum_role.value,
    }
    if spec.description:
        rendered["description"] = spec.description
    if spec.parameters:
        rendered["parameters"] = [_render_parameter(p) for p in spec.parameters]
    if spec.request_body_schema:
        rendered["body"] = _render_body(spec)
    if spec.is_write:
        rendered["side_effects"] = spec.side_effects
    return rendered


def _render_parameter(param: Any) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "name": param.name,
        "in": param.location,
        "type": param.json_type,
        "required": param.required,
    }
    if param.enum_values:
        rendered["allowed_values"] = list(param.enum_values)
    if param.description:
        rendered["description"] = param.description
    return rendered


def _render_body(spec: ToolSpec) -> dict[str, Any]:
    schema = spec.request_body_schema or {}
    required = set(spec.required_body_property_names)
    fields = []
    for name, prop in schema.get("properties", {}).items():
        entry: dict[str, Any] = {
            "name": name,
            "type": _type_of(prop),
            "required": name in required,
        }
        allowed = _enum_of(prop)
        if allowed:
            entry["allowed_values"] = allowed
        description = prop.get("description")
        if description:
            entry["description"] = " ".join(str(description).split())
        fields.append(entry)
    return {"required": spec.request_body_required, "fields": fields}


def _type_of(prop: dict[str, Any]) -> str:
    if "type" in prop:
        return str(prop["type"])
    for key in ("anyOf", "oneOf"):
        options = [o.get("type") for o in prop.get(key, []) if o.get("type") != "null"]
        if options:
            return str(options[0])
    if "enum" in prop or "const" in prop:
        return "string"
    return "any"


def _enum_of(prop: dict[str, Any]) -> list[str]:
    if "enum" in prop:
        return [str(v) for v in prop["enum"] if v is not None]
    if "const" in prop:
        return [str(prop["const"])]
    values: list[str] = []
    for key in ("anyOf", "oneOf"):
        for option in prop.get(key, []):
            values.extend(_enum_of(option))
    return values


def _closest(needle: str, candidates: Iterable[str]) -> str | None:
    """Cheap nearest-name suggestion for error messages."""
    import difflib

    matches = difflib.get_close_matches(needle, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None
