"""Resolve ``$steps.s1.data[0].id`` against recorded results.

A deliberately small path walker: dotted names and integer indexes, nothing
else. No ``eval``, no expression language, no arithmetic. The set of things a
reference can do is exactly "read a field" and "read an element", which means a
malicious reference has nothing to reach for.

Every failure is an explicit :class:`ResolutionError` with a message naming the
path that broke. A reference that silently produced ``None`` would put an empty
customer id into a live request.
"""

from __future__ import annotations

import re
from typing import Any

from nl2api.executor.state import WorkflowState
from nl2api.planner.models import StepReference

#: One path segment: ``.name`` or ``[0]``.
_SEGMENT = re.compile(r"(?:^|\.)(?P<name>[^.\[\]]+)|\[(?P<index>\d+)\]")


class ResolutionError(ValueError):
    """A reference could not be resolved against the state."""


def parse_path(path: str) -> list[str | int]:
    """Split ``data[0].id`` into ``["data", 0, "id"]``.

    Raises if the path contains anything the walker does not understand, rather
    than skipping it — a partially understood path is worse than none.
    """
    if not path:
        return []

    segments: list[str | int] = []
    position = 0
    for match in _SEGMENT.finditer(path):
        if match.start() != position:
            raise ResolutionError(f"Cannot parse reference path {path!r} at position {position}.")
        name, index = match.group("name"), match.group("index")
        segments.append(name if name is not None else int(index))  # type: ignore[arg-type]
        position = match.end()

    if position != len(path):
        raise ResolutionError(f"Cannot parse reference path {path!r} at position {position}.")
    return segments


def resolve(reference: StepReference, state: WorkflowState) -> Any:
    """Read the value a reference points at, or explain why it cannot."""
    result = state.results.get(reference.step_id)
    if result is None:
        raise ResolutionError(
            f"{reference.raw} refers to step {reference.step_id!r}, which has not run."
        )
    if not result.ok:
        raise ResolutionError(
            f"{reference.raw} refers to step {reference.step_id!r}, which "
            f"{'failed' if result.error else 'did not succeed'}."
        )

    current: Any = result.response
    walked: list[str] = [f"$steps.{reference.step_id}"]

    for segment in parse_path(reference.path):
        walked.append(f"[{segment}]" if isinstance(segment, int) else f".{segment}")
        here = "".join(walked)

        if isinstance(segment, int):
            if not isinstance(current, list):
                raise ResolutionError(f"{here}: expected a list, found {_describe(current)}.")
            if segment >= len(current):
                raise ResolutionError(
                    f"{here}: index {segment} is out of range; "
                    f"only {len(current)} item(s) were returned."
                )
            current = current[segment]
            continue

        if not isinstance(current, dict):
            raise ResolutionError(f"{here}: expected an object, found {_describe(current)}.")
        if segment not in current:
            available = ", ".join(sorted(current)[:8]) or "nothing"
            raise ResolutionError(f"{here}: no such field. Available: {available}.")
        current = current[segment]

    if current is None:
        raise ResolutionError(f"{reference.raw} resolved to null, which is not a usable value.")
    return current


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    return {dict: "an object", list: "a list", str: "a string"}.get(
        type(value), type(value).__name__
    )
