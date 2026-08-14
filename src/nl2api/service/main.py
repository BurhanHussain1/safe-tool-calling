"""The assistant's own HTTP API."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from nl2api.config import Role, get_settings
from nl2api.executor.client import ApiClient
from nl2api.mock_api.main import build_openapi
from nl2api.mock_api.main import create_app as create_mock_app
from nl2api.persistence.repository import RunRepository
from nl2api.schema.registry import ToolRegistry
from nl2api.service.assistant import Assistant, AssistantResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------
class PlanRequestBody(BaseModel):
    request: str = Field(min_length=1, description="What you want, in plain English.")
    role: Role | None = Field(default=None, description="Defaults to the configured role.")


class ApprovalBody(BaseModel):
    step_id: str
    approved: bool
    decided_by: str = Field(min_length=1)
    note: str | None = None


class ExecuteBody(BaseModel):
    request: str
    role: Role | None = None


def _serialise(result: AssistantResult) -> dict[str, Any]:
    """Render a turn for the wire — the plan, the verdict, and the reasoning."""
    plan = result.planning.plan
    return {
        "run_id": result.run_id,
        "status": result.status,
        "intent": plan.intent,
        "message": result.message,
        "clarifying_question": result.clarifying_question,
        "refusal": plan.refusal,
        "assumptions": plan.assumptions,
        "candidates": list(result.planning.candidate_ids),
        "pending_approval": list(result.pending_approval),
        "validation_errors": list(result.validation.messages),
        "steps": [
            {
                "step_id": step.id,
                "operation_id": step.operation_id,
                "reason": step.reason,
                "arguments": {a.name: a.value for a in step.arguments},
                "decision": (d.decision.label if (d := _decision(result, step.id)) else None),
                "risk": (d.risk.value if d else None),
            }
            for step in plan.steps
        ],
        "previews": [
            {
                "step_id": p.step_id,
                "summary": p.summary,
                "reversible": p.reversible,
                "warnings": list(p.warnings),
                "rendered": p.render(),
            }
            for p in result.previews
        ],
        "results": [
            {
                "step_id": r.step_id,
                "status": r.status.value,
                "status_code": r.status_code,
                "error": r.error,
            }
            for r in (result.execution.state.ordered if result.execution else ())
        ],
    }


def _decision(result: AssistantResult, step_id: str):
    return result.policy.decision_for(step_id) if result.policy else None


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------
def get_assistant(request: Request) -> Assistant:
    """The assistant for this app, taken off ``app.state``.

    Defined at module level on purpose. ``from __future__ import annotations``
    turns every annotation into a string that FastAPI resolves against *module*
    globals, so an ``Annotated[..., Depends(...)]`` alias declared inside the
    app factory is invisible to it — and a dependency it cannot resolve is
    silently treated as a query parameter, which turns every route into a 422.
    """
    return request.app.state.assistant  # type: ignore[no-any-return]


AssistantDep = Annotated[Assistant, Depends(get_assistant)]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def create_app(assistant: Assistant | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="NL → API Assistant",
        version="1.0.0",
        description=(
            "Turns natural language into validated, policy-checked API calls. "
            "High-risk actions are previewed and held for human approval."
        ),
    )

    if assistant is None:
        registry = ToolRegistry.from_openapi(build_openapi(create_mock_app()))
        assistant = Assistant(registry, client=ApiClient(settings), repository=RunRepository())
    app.state.assistant = assistant

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "assistant"}

    @app.post("/assistant/plan", summary="Plan a request and go as far as is safe")
    async def plan(body: PlanRequestBody, service: AssistantDep) -> dict[str, Any]:
        return _serialise(service.plan(body.request, role=body.role))

    @app.post("/assistant/runs/{run_id}/approve", summary="Approve or reject one step")
    async def approve(run_id: str, body: ApprovalBody, service: AssistantDep) -> dict[str, Any]:
        if service.repository.get(run_id) is None:
            raise HTTPException(404, detail={"code": "run_not_found", "message": run_id})
        service.approve(
            run_id,
            body.step_id,
            approved=body.approved,
            decided_by=body.decided_by,
            note=body.note,
        )
        return {"run_id": run_id, "step_id": body.step_id, "approved": body.approved}

    @app.post("/assistant/runs/{run_id}/execute", summary="Run an approved plan")
    async def execute(run_id: str, body: ExecuteBody, service: AssistantDep) -> dict[str, Any]:
        run = service.repository.get(run_id)
        if run is None:
            raise HTTPException(404, detail={"code": "run_not_found", "message": run_id})
        role = body.role or Role(run.role)
        return _serialise(service.execute(run_id, body.request, role))

    @app.get("/assistant/runs", summary="Recent runs")
    async def list_runs(service: AssistantDep, limit: int = 25) -> dict[str, Any]:
        runs = service.repository.list_runs(limit)
        return {
            "data": [
                {
                    "run_id": r.id,
                    "request": r.request,
                    "role": r.role,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                    "steps": len(r.steps),
                }
                for r in runs
            ],
            "total": len(runs),
        }

    @app.get("/assistant/runs/{run_id}", summary="The full audit trail for one run")
    async def get_run(run_id: str, service: AssistantDep) -> dict[str, Any]:
        run = service.repository.get(run_id)
        if run is None:
            raise HTTPException(404, detail={"code": "run_not_found", "message": run_id})
        return {
            "run_id": run.id,
            "request": run.request,
            "role": run.role,
            "status": run.status,
            "intent": run.intent,
            "final_answer": run.final_answer,
            "clarifying_question": run.clarifying_question,
            "refusal": run.refusal,
            "candidates": run.candidates,
            "steps": [
                {
                    "step_id": s.step_id,
                    "operation_id": s.operation_id,
                    "method": s.method,
                    "path": s.path,
                    "risk": s.risk,
                    "arguments": s.arguments,
                    "validation": s.validation,
                    "decision": s.decision,
                    "decision_reasons": s.decision_reasons,
                    "dry_run": s.dry_run,
                    "status": s.status,
                    "status_code": s.status_code,
                    "error": s.error,
                }
                for s in run.steps
            ],
            "approvals": [
                {
                    "step_id": a.step_id,
                    "decision": a.decision,
                    "decided_by": a.decided_by,
                    "decided_at": a.decided_at.isoformat(),
                    "note": a.note,
                }
                for a in run.approvals
            ],
            "events": [
                {"kind": e.kind, "payload": e.payload, "at": e.created_at.isoformat()}
                for e in run.events
            ],
        }

    return app


app = create_app()
