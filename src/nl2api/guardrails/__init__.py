"""The layer that decides whether a proposed plan may run.

    validator.py   does the plan match the API's schema?
    policy.py      is this caller allowed, and how dangerous is it?
    dryrun.py      what would a write actually do, in plain English?
    redaction.py   strip anything that should not reach a log

Nothing here knows about the language model, and nothing here can be influenced
by it. The planner produces a proposal; this module is the only thing that
decides whether the proposal becomes a request.

Order matters: validate, then apply policy, then preview. A plan that fails
validation is never policy-checked, and a plan that policy denies is never
previewed — so an invalid or forbidden step cannot reach even a read-only probe.
"""

from nl2api.guardrails.dryrun import DryRunPreview, StateReader, build_previews
from nl2api.guardrails.policy import (
    Decision,
    PolicyEngine,
    PolicyReport,
    StepDecision,
)
from nl2api.guardrails.redaction import redact, redact_text
from nl2api.guardrails.validator import (
    FieldError,
    PlanValidator,
    ResolvedCall,
    ValidationReport,
    coerce,
)

__all__ = [
    "Decision",
    "DryRunPreview",
    "FieldError",
    "PlanValidator",
    "PolicyEngine",
    "PolicyReport",
    "ResolvedCall",
    "StateReader",
    "StepDecision",
    "ValidationReport",
    "build_previews",
    "coerce",
    "redact",
    "redact_text",
]
