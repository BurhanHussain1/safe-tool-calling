"""Phase 5 tests: the assistant service and the audit trail.

The requirement being checked: a run must be reconstructible from the audit
tables alone — the request, the plan, the verdict, the preview, the approval and
the response, without reading application logs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nl2api.config import Role, Settings
from nl2api.executor.client import ApiClient
from nl2api.mock_api.main import build_openapi
from nl2api.mock_api.main import create_app as create_mock_app
from nl2api.mock_api.store import store
from nl2api.persistence.db import build_engine
from nl2api.persistence.models import Base
from nl2api.persistence.repository import RunRepository
from nl2api.planner.llm import RuleBasedBackend
from nl2api.planner.planner import Planner
from nl2api.schema.registry import ToolRegistry
from nl2api.service.assistant import Assistant
from nl2api.service.main import create_app as create_service_app


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry.from_openapi(build_openapi(create_mock_app()))


@pytest.fixture(autouse=True)
def _reset_store() -> Iterator[None]:
    store.reset()
    yield
    store.reset()


@pytest.fixture
def repository(tmp_path: Path) -> RunRepository:
    engine = build_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)
    return RunRepository(engine)


@pytest.fixture
def assistant(
    registry: ToolRegistry, repository: RunRepository, tmp_path: Path
) -> Iterator[Assistant]:
    settings = Settings(
        llm_provider="rules",
        database_url=f"sqlite:///{tmp_path / 'audit.db'}",
        _env_file=None,  # type: ignore[call-arg]
    )
    with TestClient(create_mock_app()) as http:
        yield Assistant(
            registry,
            client=ApiClient(settings, client=http, actor="tests"),
            repository=repository,
            planner=Planner(registry, backend=RuleBasedBackend(), settings=settings),
            settings=settings,
        )


@pytest.fixture
def api(assistant: Assistant) -> Iterator[TestClient]:
    with TestClient(create_service_app(assistant)) as client:
        yield client


REFUND = "refund invoice INV-1007 because they were charged twice"


class TestAuditTrail:
    def test_a_read_run_is_fully_recorded(self, assistant: Assistant) -> None:
        result = assistant.plan("show me customer CUS-1001", role=Role.VIEWER)
        run = assistant.repository.get(result.run_id)

        assert run is not None
        assert run.status == "completed"
        assert run.role == "viewer"
        assert run.candidates, "the offered shortlist must be recorded"
        assert len(run.steps) == 1
        assert run.steps[0].status == "ok"
        assert run.steps[0].status_code == 200
        assert {e.kind for e in run.events} >= {"planned", "run_completed"}

    def test_a_gated_run_records_the_preview_before_any_approval(
        self, assistant: Assistant
    ) -> None:
        result = assistant.plan(REFUND, role=Role.BILLING_ADMIN)
        assert result.needs_approval

        run = assistant.repository.get(result.run_id)
        assert run is not None
        assert run.status == "awaiting_approval"

        refund_step = next(s for s in run.steps if s.operation_id == "create_refund")
        assert refund_step.decision == "REQUIRE_APPROVAL"
        assert refund_step.decision_reasons
        assert refund_step.dry_run is not None
        assert refund_step.dry_run["reversible"] is False
        assert refund_step.status is None, "nothing should have been executed"

    def test_a_refusal_is_recorded_with_its_reason(self, assistant: Assistant) -> None:
        result = assistant.plan("delete all customers", role=Role.BILLING_ADMIN)
        run = assistant.repository.get(result.run_id)
        assert run is not None
        assert run.status == "refused"
        assert run.refusal
        assert run.steps == []

    def test_a_clarifying_question_is_recorded(self, assistant: Assistant) -> None:
        result = assistant.plan("issue a refund", role=Role.BILLING_ADMIN)
        run = assistant.repository.get(result.run_id)
        assert run is not None
        assert run.status == "needs_clarification"
        assert run.clarifying_question

    def test_emails_are_masked_in_the_audit_trail(self, assistant: Assistant) -> None:
        """Redaction happens on write, so the raw address is never stored."""
        result = assistant.plan("look up ana@acme.io", role=Role.VIEWER)
        run = assistant.repository.get(result.run_id)
        assert run is not None
        assert "ana@acme.io" not in run.request
        assert "@acme.io" in run.request, "enough must survive to correlate records"


class TestApprovalLifecycle:
    def test_approval_is_persisted_and_gates_execution(self, assistant: Assistant) -> None:
        planned = assistant.plan(REFUND, role=Role.BILLING_ADMIN)
        assert assistant.repository.approved_steps(planned.run_id) == frozenset()

        for step_id in planned.pending_approval:
            assistant.approve(planned.run_id, step_id, approved=True, decided_by="lead@example.com")
        assert assistant.repository.approved_steps(planned.run_id) == set(planned.pending_approval)

        executed = assistant.execute(planned.run_id, REFUND, Role.BILLING_ADMIN)
        assert executed.status == "completed", executed.message
        assert store.get_invoice("INV-1007").status == "refunded"

    def test_a_rejection_leaves_nothing_approved(self, assistant: Assistant) -> None:
        planned = assistant.plan(REFUND, role=Role.BILLING_ADMIN)
        for step_id in planned.pending_approval:
            assistant.approve(
                planned.run_id, step_id, approved=False, decided_by="lead@example.com"
            )

        assert assistant.repository.approved_steps(planned.run_id) == frozenset()
        resumed = assistant.execute(planned.run_id, REFUND, Role.BILLING_ADMIN)
        assert resumed.status == "awaiting_approval"
        assert store.get_invoice("INV-1007").refunded_cents == 0

    def test_the_decision_maker_is_recorded(self, assistant: Assistant) -> None:
        planned = assistant.plan(REFUND, role=Role.BILLING_ADMIN)
        assistant.approve(
            planned.run_id,
            planned.pending_approval[0],
            approved=True,
            decided_by="lead@example.com",
            note="Confirmed with billing.",
        )
        run = assistant.repository.get(planned.run_id)
        assert run is not None
        approval = run.approvals[0]
        assert approval.decision == "approved"
        assert approval.note == "Confirmed with billing."
        assert approval.decided_at is not None


class TestServiceApi:
    def test_plan_endpoint_returns_the_preview_and_gate(self, api: TestClient) -> None:
        response = api.post("/assistant/plan", json={"request": REFUND, "role": "billing_admin"})
        assert response.status_code == 200
        body = response.json()

        assert body["status"] == "awaiting_approval"
        assert body["pending_approval"]
        assert body["previews"]
        assert "cannot be undone" in body["previews"][0]["rendered"]
        assert [s["operation_id"] for s in body["steps"]] == ["get_invoice", "create_refund"]

    def test_full_approve_then_execute_over_http(self, api: TestClient) -> None:
        planned = api.post(
            "/assistant/plan", json={"request": REFUND, "role": "billing_admin"}
        ).json()
        run_id = planned["run_id"]

        approve = api.post(
            f"/assistant/runs/{run_id}/approve",
            json={
                "step_id": planned["pending_approval"][0],
                "approved": True,
                "decided_by": "lead@example.com",
            },
        )
        assert approve.status_code == 200

        executed = api.post(
            f"/assistant/runs/{run_id}/execute",
            json={"request": REFUND, "role": "billing_admin"},
        ).json()
        assert executed["status"] == "completed", executed["message"]
        assert store.get_invoice("INV-1007").status == "refunded"

    def test_refusal_over_http_performs_no_write(self, api: TestClient) -> None:
        before = store.snapshot()
        body = api.post(
            "/assistant/plan",
            json={"request": "delete all customers", "role": "billing_admin"},
        ).json()
        assert body["status"] == "refused"
        assert body["refusal"]
        assert store.snapshot() == before

    def test_run_detail_exposes_the_whole_trail(self, api: TestClient) -> None:
        run_id = api.post(
            "/assistant/plan", json={"request": REFUND, "role": "billing_admin"}
        ).json()["run_id"]

        detail = api.get(f"/assistant/runs/{run_id}").json()
        assert detail["run_id"] == run_id
        assert detail["candidates"]
        assert detail["steps"][1]["dry_run"]["reversible"] is False
        assert detail["events"]

    def test_unknown_run_is_a_404(self, api: TestClient) -> None:
        assert api.get("/assistant/runs/run_missing").status_code == 404

    def test_runs_can_be_listed(self, api: TestClient) -> None:
        api.post("/assistant/plan", json={"request": "show me customer CUS-1001", "role": "viewer"})
        listing = api.get("/assistant/runs").json()
        assert listing["total"] >= 1
        assert listing["data"][0]["run_id"].startswith("run_")

    def test_health(self, api: TestClient) -> None:
        assert api.get("/health").json()["status"] == "ok"
