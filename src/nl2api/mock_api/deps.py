"""Shared FastAPI dependencies: caller identity and role enforcement.

The mock API enforces roles itself even though the assistant's policy engine
(Phase 3) rejects unauthorised plans earlier. That redundancy is the point — a
guardrail you can bypass by calling the API directly is not a guardrail. It also
lets the golden tests assert that an under-privileged plan fails at *both* layers.

Identity comes from an ``X-Role`` header. A real system would use a signed token;
that would add ceremony without changing anything this project demonstrates.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from nl2api.config import Role, get_settings

ROLE_HEADER = "X-Role"
ACTOR_HEADER = "X-Actor"


def get_role(x_role: Annotated[str | None, Header(alias=ROLE_HEADER)] = None) -> Role:
    """Resolve the caller's role, falling back to the configured default."""
    if x_role is None:
        return get_settings().default_role
    try:
        return Role(x_role.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "unknown_role",
                "message": (
                    f"Unknown role {x_role!r}. Expected one of {', '.join(r.value for r in Role)}."
                ),
            },
        ) from None


def get_actor(x_actor: Annotated[str | None, Header(alias=ACTOR_HEADER)] = None) -> str:
    """Who to record as having performed a write."""
    return (x_actor or "assistant").strip() or "assistant"


#: Reusable annotations so routes read as data, not plumbing.
CurrentRole = Annotated[Role, Depends(get_role)]
CurrentActor = Annotated[str, Depends(get_actor)]


def require_role(minimum: Role) -> Callable[[Role], Role]:
    """Dependency factory that rejects callers below ``minimum``.

    Declared in the route's ``dependencies=[...]`` so the check runs before the
    handler body and the handler stays free of auth code::

        @router.post(
            "/refunds",
            dependencies=[Depends(require_role(Role.BILLING_ADMIN))],
        )
    """

    def dependency(role: CurrentRole) -> Role:
        if not role.implies(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "insufficient_role",
                    "message": (
                        f"This operation requires the {minimum.value!r} role or higher; "
                        f"caller has {role.value!r}."
                    ),
                },
            )
        return role

    return dependency
