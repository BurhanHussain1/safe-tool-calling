"""Turn a natural-language request into a strictly typed call plan.

    models.py    Plan / PlanStep / Argument — what the model is allowed to say
    prompts.py   the system prompt and per-request assembly
    llm.py       backends: Anthropic, cassette replay, offline rules
    planner.py   orchestration and structural checks

The planner produces proposals. It never decides whether a proposal is safe —
that is :mod:`nl2api.guardrails`, deliberately a separate module with no
knowledge of the model.
"""

from nl2api.planner.llm import (
    AnthropicBackend,
    BackendError,
    CassetteBackend,
    PlannerBackend,
    PlanRequest,
    RuleBasedBackend,
    build_backend,
)
from nl2api.planner.models import Argument, Plan, PlanStep, StepReference, parse_reference
from nl2api.planner.planner import Planner, PlanningResult, build_planner

__all__ = [
    "AnthropicBackend",
    "Argument",
    "BackendError",
    "CassetteBackend",
    "Plan",
    "PlanRequest",
    "PlanStep",
    "Planner",
    "PlannerBackend",
    "PlanningResult",
    "RuleBasedBackend",
    "StepReference",
    "build_backend",
    "build_planner",
    "parse_reference",
]
