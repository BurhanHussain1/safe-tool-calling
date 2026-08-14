"""Orchestration: request in, plan out.

The planner does three things and nothing else:

1. Trim the catalogue to what the caller may actually call.
2. Shortlist the operations relevant to the request.
3. Ask the backend for a plan, then check the *structural* claims it makes —
   that every operation it named exists and was actually offered.

It does not decide whether the plan is safe. Parameter validation, permissions
and approval gates are Phase 3's job, deliberately separate: this module knows
about the model, that one knows about policy, and neither has to trust the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from nl2api.config import Role, Settings, get_settings
from nl2api.planner.llm import PlannerBackend, PlanRequest, build_backend
from nl2api.planner.models import Plan
from nl2api.schema.registry import ToolRegistry
from nl2api.schema.retriever import BM25Retriever, Retriever, ScoredTool

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanningResult:
    """A plan plus how it was arrived at.

    The retrieval trace is kept because "why was this endpoint even an option?"
    is the first question asked when a plan looks wrong, and reconstructing it
    after the fact is guesswork.
    """

    plan: Plan
    request: str
    role: Role
    candidates: tuple[ScoredTool, ...]
    structural_errors: tuple[str, ...] = field(default=())

    @property
    def is_structurally_valid(self) -> bool:
        return not self.structural_errors

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(c.operation_id for c in self.candidates)


class Planner:
    """Turns a natural-language request into a :class:`Plan`."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        backend: PlannerBackend | None = None,
        retriever: Retriever | None = None,
        settings: Settings | None = None,
        cassette_directory: Path | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings or get_settings()
        self.retriever = retriever or BM25Retriever(registry)
        self.backend = backend or build_backend(
            self.settings, cassette_directory=cassette_directory
        )

    def plan(
        self, request: str, *, role: Role | None = None, top_k: int | None = None
    ) -> PlanningResult:
        """Produce a plan for ``request`` as seen by ``role``."""
        role = role or self.settings.default_role
        k = top_k or self.settings.retriever_top_k

        candidates = self._shortlist(request, role, k)
        if not candidates:
            # Nothing in the catalogue relates to the request. Saying so is a far
            # better answer than handing the model an arbitrary set of endpoints
            # and hoping it declines to use them.
            return PlanningResult(
                plan=Plan.asking(
                    intent=request,
                    question=(
                        "I could not find an operation that matches that request. "
                        "Could you rephrase it, or tell me which record it concerns?"
                    ),
                ),
                request=request,
                role=role,
                candidates=(),
            )

        candidate_ids = tuple(c.operation_id for c in candidates)
        plan_request = PlanRequest(
            request=request,
            role=role,
            catalogue=self.registry.render_for_prompt(candidate_ids),
            candidate_operation_ids=candidate_ids,
        )

        plan = self.backend.generate(plan_request)
        errors = self._structural_errors(plan, candidate_ids, role)
        if errors:
            logger.warning("Plan for %r failed structural checks: %s", request, "; ".join(errors))

        return PlanningResult(
            plan=plan,
            request=request,
            role=role,
            candidates=tuple(candidates),
            structural_errors=tuple(errors),
        )

    # -- internals ---------------------------------------------------------
    def _shortlist(self, request: str, role: Role, k: int) -> list[ScoredTool]:
        """Rank operations, then drop any this caller could never call.

        Filtering after ranking rather than before keeps the scores comparable
        across roles, which makes a surprising shortlist easier to debug.

        We over-fetch so that role filtering does not leave a thin shortlist,
        then trim back to ``k`` — but resolver candidates must survive the trim.
        They rank last by design, so a naive ``[:k]`` discards exactly the
        lookup endpoint the retriever appended on purpose, and a correct
        multi-step plan then references an operation that was never offered.
        """
        ranked = self.retriever.top_k(request, k=k * 2)
        permitted = [c for c in ranked if c.spec.permits(role)]
        if len(permitted) < len(ranked):
            dropped = [c.operation_id for c in ranked if not c.spec.permits(role)]
            logger.debug("Dropped %s from the shortlist: role %s cannot call them.", dropped, role)

        head = permitted[:k]
        head_ids = {c.operation_id for c in head}
        resolver_ids = {spec.operation_id for spec in self.retriever.resolver_candidates(request)}
        rescued = [
            c
            for c in permitted
            if c.operation_id in resolver_ids and c.operation_id not in head_ids
        ]
        if not rescued:
            return head
        return [*head[: max(k - len(rescued), 1)], *rescued][:k]

    def _structural_errors(self, plan: Plan, offered: tuple[str, ...], role: Role) -> list[str]:
        """Check the claims a plan makes about *itself*, not about safety.

        Three failures matter here and nowhere else:

        * a hallucinated operation id — the endpoint does not exist;
        * an operation that exists but was never offered — the model reached
          outside the shortlist, which is the signal we care about for injection;
        * an operation this role cannot call — caught again by the policy engine,
          but catching it here means a bad plan never reaches a dry run.
        """
        errors: list[str] = []
        offered_set = set(offered)

        for step in plan.steps:
            operation_id = step.operation_id
            if operation_id not in self.registry:
                errors.append(
                    f"Step {step.id}: operation {operation_id!r} does not exist in the API."
                )
                continue
            if operation_id not in offered_set:
                errors.append(
                    f"Step {step.id}: operation {operation_id!r} was not among the "
                    f"operations offered for this request."
                )
            spec = self.registry.get(operation_id)
            if not spec.permits(role):
                errors.append(
                    f"Step {step.id}: {operation_id!r} needs the "
                    f"{spec.minimum_role.value!r} role; caller has {role.value!r}."
                )

        return errors


def build_planner(document: dict, *, settings: Settings | None = None, **kwargs: object) -> Planner:
    """Convenience constructor from a raw OpenAPI document."""
    return Planner(ToolRegistry.from_openapi(document), settings=settings, **kwargs)  # type: ignore[arg-type]
