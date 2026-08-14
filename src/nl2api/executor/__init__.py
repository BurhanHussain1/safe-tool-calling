"""Run an approved plan against the business API.

    state.py     typed workflow state; the only channel between steps
    resolver.py  $steps.… path walking, with no expression language
    client.py    HTTP transport, retrying only what is provably safe
    engine.py    the step loop: resolve, re-check, gate, send, or halt

This is the only layer permitted to change data, and it earns that by
re-validating everything it resolves before sending it.
"""

from nl2api.executor.client import ApiClient, ApiResponse, TransportError
from nl2api.executor.engine import IDENTITY_OPERATIONS, ExecutionResult, WorkflowEngine
from nl2api.executor.resolver import ResolutionError, parse_path, resolve
from nl2api.executor.state import StepResult, StepStatus, WorkflowState

__all__ = [
    "IDENTITY_OPERATIONS",
    "ApiClient",
    "ApiResponse",
    "ExecutionResult",
    "ResolutionError",
    "StepResult",
    "StepStatus",
    "TransportError",
    "WorkflowEngine",
    "WorkflowState",
    "parse_path",
    "resolve",
]
