"""Risk and permission metadata attached to endpoints.

The ``@risk`` decorator stamps metadata onto the endpoint function. At startup
:func:`nl2api.mock_api.main.build_openapi` walks the route table and injects that
metadata into the generated OpenAPI document as vendor extensions:

    x-risk             read_only | low_risk_write | high_risk_write
    x-required-roles   the minimum role, plus every role that implies it
    x-side-effects     one plain-English sentence, written for a human reviewer
    x-idempotent       whether repeating the call is safe

The assistant reads those extensions from the same document it reads parameters
from, so the contract has exactly one source of truth. A reviewer can also just
``curl /openapi.json`` and see the entire security posture of the system.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from nl2api.config import Role

#: Attribute name used to stash metadata on the endpoint function.
RISK_ATTRIBUTE = "__risk_metadata__"


class RiskLevel(StrEnum):
    """How much damage an operation can do.

    The assistant maps these to actions: read-only executes immediately,
    low-risk writes execute but are reported, and high-risk writes are dry-run
    and blocked until a human approves.
    """

    READ_ONLY = "read_only"
    LOW_RISK_WRITE = "low_risk_write"
    HIGH_RISK_WRITE = "high_risk_write"

    @property
    def is_write(self) -> bool:
        return self is not RiskLevel.READ_ONLY

    @property
    def needs_approval(self) -> bool:
        return self is RiskLevel.HIGH_RISK_WRITE


@dataclass(frozen=True, slots=True)
class RiskMetadata:
    """Everything the assistant needs to know about an operation's danger."""

    level: RiskLevel
    minimum_role: Role
    side_effects: str
    idempotent: bool

    @property
    def allowed_roles(self) -> tuple[Role, ...]:
        """Every role that satisfies :attr:`minimum_role`.

        Expanded here rather than left implicit so the published contract states
        the full allow-list. A consumer should not have to know our role
        hierarchy to read the document correctly.
        """
        return tuple(role for role in Role if role.implies(self.minimum_role))

    def as_openapi_extensions(self) -> dict[str, Any]:
        return {
            "x-risk": self.level.value,
            "x-required-roles": [role.value for role in self.allowed_roles],
            "x-side-effects": self.side_effects,
            "x-idempotent": self.idempotent,
        }


F = TypeVar("F", bound=Callable[..., Any])


def risk(
    level: RiskLevel,
    *,
    minimum_role: Role,
    side_effects: str,
    idempotent: bool = False,
) -> Callable[[F], F]:
    """Declare an endpoint's risk level, required role, and side effects.

    Apply it directly above the function so it runs before the router
    decorator::

        @router.post("/refunds", ...)
        @risk(
            RiskLevel.HIGH_RISK_WRITE,
            minimum_role=Role.BILLING_ADMIN,
            side_effects="Moves money back to the customer. Not reversible.",
        )
        async def create_refund(...): ...

    ``side_effects`` is shown to a human during the approval step, so write it
    as a sentence a support lead would understand — not as a schema comment.
    """
    if not side_effects.strip():
        raise ValueError("side_effects must describe what the operation does")

    metadata = RiskMetadata(
        level=level,
        minimum_role=minimum_role,
        side_effects=side_effects.strip(),
        idempotent=idempotent,
    )

    def decorator(func: F) -> F:
        setattr(func, RISK_ATTRIBUTE, metadata)
        return func

    return decorator


def get_risk_metadata(func: Callable[..., Any]) -> RiskMetadata | None:
    """Read back metadata stamped by :func:`risk`, if any."""
    return getattr(func, RISK_ATTRIBUTE, None)
