"""Phase 4 tests: running a plan without ever guessing.

The executor is the only layer allowed to change data, so these tests are
mostly about the situations where it must decline to: an ambiguous lookup, a
value that failed re-validation, an unapproved high-risk write, a failed step
with work queued behind it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from nl2api.config import Role, Settings
from nl2api.executor.client import ApiClient
from nl2api.executor.engine import ExecutionResult, WorkflowEngine
from nl2api.executor.resolver import ResolutionError, parse_path, resolve
from nl2api.executor.state import StepResult, StepStatus, WorkflowState
from nl2api.guardrails.validator import PlanValidator
from nl2api.mock_api.main import build_openapi, create_app
from nl2api.mock_api.store import store
from nl2api.planner.models import Argument, Plan, PlanStep, parse_reference
from nl2api.schema.registry import ToolRegistry


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry.from_openapi(build_openapi(create_app()))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _reset_store() -> Iterator[None]:
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client(settings: Settings) -> Iterator[ApiClient]:
    """A client wired straight to the app — real requests, no network.

    ``TestClient`` is an ``httpx.Client`` subclass, so the executor's transport
    is exercised for real; only the socket is replaced.
    """
    with TestClient(create_app()) as http:
        yield ApiClient(settings, client=http, actor="test-suite")


@pytest.fixture
def engine(registry: ToolRegistry, client: ApiClient, settings: Settings) -> WorkflowEngine:
    return WorkflowEngine(registry, client, settings=settings)


@pytest.fixture
def validator(registry: ToolRegistry) -> PlanValidator:
    return PlanValidator(registry)


def _step(step_id: str, operation_id: str, args: list[tuple[str, str, str]]) -> PlanStep:
    return PlanStep(
        id=step_id,
        operation_id=operation_id,
        arguments=[
            Argument(name=n, location=loc, value=v)  # type: ignore[arg-type]
            for n, loc, v in args
        ],
        reason="Because.",
        expected_result="A thing.",
    )


def _run(
    engine: WorkflowEngine,
    validator: PlanValidator,
    *steps: PlanStep,
    role: Role = Role.BILLING_ADMIN,
    approved: frozenset[str] = frozenset(),
) -> ExecutionResult:
    report = validator.validate(Plan(intent="test", steps=list(steps)))
    assert report.ok, report.messages
    return engine.execute(report.calls, role, run_id="run-1", approved_steps=approved)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
class TestPathParsing:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("id", ["id"]),
            ("data[0].id", ["data", 0, "id"]),
            ("a.b.c", ["a", "b", "c"]),
            ("data[10]", ["data", 10]),
            ("", []),
        ],
    )
    def test_valid_paths(self, path: str, expected: list[str | int]) -> None:
        assert parse_path(path) == expected

    @pytest.mark.parametrize("path", ["data[", "data]0[", "a..b", "a[x]"])
    def test_malformed_paths_are_rejected_not_skipped(self, path: str) -> None:
        """A partially understood path is worse than none."""
        with pytest.raises(ResolutionError):
            parse_path(path)


class TestResolution:
    def _state(self, response: object) -> WorkflowState:
        state = WorkflowState(run_id="r", role=Role.VIEWER)
        state.record(
            StepResult(
                step_id="s1",
                operation_id="search_customers",
                status=StepStatus.OK,
                status_code=200,
                response=response,
            )
        )
        return state

    def _ref(self, raw: str):
        reference = parse_reference(raw)
        assert reference is not None
        return reference

    def test_reads_a_nested_value(self) -> None:
        state = self._state({"data": [{"id": "CUS-1001", "name": "Ana"}], "total": 1})
        assert resolve(self._ref("$steps.s1.data[0].id"), state) == "CUS-1001"

    def test_missing_step_is_named(self) -> None:
        with pytest.raises(ResolutionError, match="has not run"):
            resolve(self._ref("$steps.s9.id"), self._state({}))

    def test_index_out_of_range_says_how_many_there_were(self) -> None:
        state = self._state({"data": [], "total": 0})
        with pytest.raises(ResolutionError, match="only 0 item"):
            resolve(self._ref("$steps.s1.data[0].id"), state)

    def test_unknown_field_lists_what_is_available(self) -> None:
        state = self._state({"data": [{"id": "CUS-1001"}]})
        with pytest.raises(ResolutionError, match="Available: id"):
            resolve(self._ref("$steps.s1.data[0].email"), state)

    def test_indexing_a_non_list_is_an_error(self) -> None:
        with pytest.raises(ResolutionError, match="expected a list"):
            resolve(self._ref("$steps.s1.data[0]"), self._state({"data": "not a list"}))

    def test_null_is_never_a_usable_value(self) -> None:
        """Silently resolving to None would put an empty id into a live request."""
        state = self._state({"data": [{"id": None}]})
        with pytest.raises(ResolutionError, match="null"):
            resolve(self._ref("$steps.s1.data[0].id"), state)

    def test_referencing_a_failed_step_is_refused(self) -> None:
        state = WorkflowState(run_id="r", role=Role.VIEWER)
        state.record(
            StepResult(
                step_id="s1",
                operation_id="get_invoice",
                status=StepStatus.FAILED,
                error="boom",
            )
        )
        with pytest.raises(ResolutionError, match="failed"):
            resolve(self._ref("$steps.s1.id"), state)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
class TestExecution:
    def test_single_read_runs(self, engine: WorkflowEngine, validator: PlanValidator) -> None:
        result = _run(
            engine,
            validator,
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            role=Role.VIEWER,
        )
        assert result.completed
        assert result.state.results["s1"].response["amount_cents"] == 24000

    def test_four_step_chain_passes_ids_between_steps(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        """The canonical multi-step workflow, end to end."""
        result = _run(
            engine,
            validator,
            _step("s1", "search_customers", [("email", "query", "dana@northwind.co")]),
            _step(
                "s2",
                "list_customer_subscriptions",
                [("customer_id", "path", "$steps.s1.data[0].id")],
            ),
            _step(
                "s3",
                "list_invoices",
                [("customer_id", "query", "$steps.s1.data[0].id"), ("status", "query", "open")],
            ),
            _step(
                "s4",
                "create_ticket",
                [
                    ("customer_id", "body", "$steps.s1.data[0].id"),
                    ("subject", "body", "Outstanding invoice follow-up"),
                    ("body", "body", "Checking in about the open invoice."),
                ],
            ),
            role=Role.BILLING_ADMIN,
        )
        assert result.completed, result.message
        assert result.state.results["s2"].response["data"][0]["customer_id"] == "CUS-1002"
        assert result.state.results["s4"].response["customer_id"] == "CUS-1002"
        assert result.state.results["s4"].status_code == 201

    def test_approved_refund_actually_moves_money(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "24000"),
                    ("reason", "body", "Duplicate charge"),
                ],
            ),
            approved=frozenset({"s1"}),
        )
        assert result.completed, result.message
        assert store.get_invoice("INV-1007").status == "refunded"


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
class TestAmbiguityGate:
    def test_multiple_matches_ask_instead_of_taking_the_first(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        """Two customers are named Ana Ruiz. Guessing would refund the wrong one."""
        result = _run(
            engine,
            validator,
            _step("s1", "search_customers", [("name", "query", "Ana Ruiz")]),
            _step("s2", "list_invoices", [("customer_id", "query", "$steps.s1.data[0].id")]),
        )
        assert result.status == "halted"
        assert result.needs_clarification
        assert "matched 2 records" in result.clarifying_question  # type: ignore[operator]
        assert "CUS-1001" in result.clarifying_question  # type: ignore[operator]
        assert result.state.results["s2"].status is StepStatus.AMBIGUOUS

    def test_no_matches_halts_with_a_useful_message(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step("s1", "search_customers", [("email", "query", "nobody@nowhere.io")]),
            _step("s2", "list_invoices", [("customer_id", "query", "$steps.s1.data[0].id")]),
        )
        assert result.status == "halted"
        assert result.needs_clarification
        assert "could not find" in result.clarifying_question.lower()  # type: ignore[union-attr]

    def test_selecting_the_newest_from_a_list_is_not_ambiguous(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        """'The last invoice' really is data[0]; only identity lookups must be unique."""
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "list_invoices",
                [("customer_id", "query", "CUS-1001"), ("status", "query", "paid")],
            ),
            _step("s2", "get_invoice", [("invoice_id", "path", "$steps.s1.data[0].id")]),
            role=Role.VIEWER,
        )
        assert result.completed, result.message
        assert result.state.results["s1"].response["total"] > 1

    def test_no_write_happens_when_a_lookup_is_ambiguous(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        """The property that matters: ambiguity costs nothing."""
        before = store.snapshot()
        result = _run(
            engine,
            validator,
            _step("s1", "search_customers", [("name", "query", "Ana Ruiz")]),
            _step(
                "s2",
                "create_ticket",
                [
                    ("customer_id", "body", "$steps.s1.data[0].id"),
                    ("subject", "body", "Ambiguous ticket"),
                    ("body", "body", "Should never be created."),
                ],
            ),
        )
        assert result.needs_clarification
        assert store.snapshot() == before


class TestApprovalGate:
    def test_unapproved_high_risk_write_suspends_the_run(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        before = store.snapshot()
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "24000"),
                    ("reason", "body", "Duplicate charge"),
                ],
            ),
        )
        assert result.status == "awaiting_approval"
        assert result.pending_approval == ("s1",)
        assert store.snapshot() == before, "an unapproved write reached the API"

    def test_resuming_after_approval_gives_the_same_result(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        refund = _step(
            "s1",
            "create_refund",
            [
                ("invoice_id", "body", "INV-1007"),
                ("amount_cents", "body", "10000"),
                ("reason", "body", "Goodwill"),
            ],
        )
        assert _run(engine, validator, refund).status == "awaiting_approval"

        resumed = _run(engine, validator, refund, approved=frozenset({"s1"}))
        assert resumed.completed
        assert store.get_invoice("INV-1007").refunded_cents == 10000

    def test_approving_one_step_does_not_approve_another(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "cancel_subscription",
                [("subscription_id", "path", "SUB-2001"), ("reason", "body", "Asked to")],
            ),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "100"),
                    ("reason", "body", "Goodwill"),
                ],
            ),
            approved=frozenset({"s1"}),
        )
        assert result.status == "awaiting_approval"
        assert result.pending_approval == ("s2",)
        assert store.list_refunds() == []

    def test_denied_by_role_never_reaches_the_api(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        before = store.snapshot()
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "100"),
                    ("reason", "body", "Nope"),
                ],
            ),
            role=Role.SUPPORT_AGENT,
            approved=frozenset({"s1"}),
        )
        assert result.status == "blocked"
        assert "billing_admin" in result.message
        assert store.snapshot() == before

    def test_low_risk_writes_need_no_approval(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_ticket",
                [
                    ("customer_id", "body", "CUS-1001"),
                    ("subject", "body", "Quick question"),
                    ("body", "body", "Nothing dangerous."),
                ],
            ),
            role=Role.SUPPORT_AGENT,
        )
        assert result.completed, result.message


class TestResolvedValueRechecks:
    def test_a_deferred_refund_over_the_ceiling_is_caught_after_resolution(
        self, registry: ToolRegistry, client: ApiClient, validator: PlanValidator
    ) -> None:
        """The check Phase 3 honestly could not make: the amount exists now."""
        settings = Settings(refund_max_cents=20_000, _env_file=None)  # type: ignore[call-arg]
        engine = WorkflowEngine(registry, client, settings=settings)
        before = store.snapshot()

        result = _run(
            engine,
            validator,
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "$steps.s1.amount_cents"),
                    ("reason", "body", "Full refund"),
                ],
            ),
            approved=frozenset({"s2"}),
        )
        assert result.status == "blocked"
        assert "ceiling" in result.message
        assert store.snapshot() == before, "the ceiling was breached anyway"

    def test_a_resolved_value_of_the_wrong_type_is_rejected(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        """An API response is no more trusted than the model."""
        result = _run(
            engine,
            validator,
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "$steps.s1.status"),  # a string, not cents
                    ("reason", "body", "Wrong type"),
                ],
            ),
            approved=frozenset({"s2"}),
        )
        assert result.status == "halted"
        assert "invalid" in result.message


class TestFailureHandling:
    def test_a_failed_step_skips_the_rest(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1003"),  # an open invoice
                    ("amount_cents", "body", "100"),
                    ("reason", "body", "Not refundable"),
                ],
            ),
            _step("s2", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            approved=frozenset({"s1"}),
        )
        assert result.status == "failed"
        assert "invoice_not_refundable" in result.message
        assert result.state.results["s2"].status is StepStatus.SKIPPED

    def test_a_business_rule_rejection_is_reported_in_plain_terms(
        self, engine: WorkflowEngine, validator: PlanValidator
    ) -> None:
        result = _run(
            engine,
            validator,
            _step(
                "s1",
                "create_refund",
                [
                    # Below the $500 policy ceiling, so this reaches the API and is
                    # rejected by the *business rule* rather than by policy.
                    ("invoice_id", "body", "INV-1007"),
                    ("amount_cents", "body", "30000"),
                    ("reason", "body", "More than the invoice"),
                ],
            ),
            approved=frozenset({"s1"}),
        )
        assert result.status == "failed"
        assert "refund_exceeds_balance" in result.message
        assert "only $240.00 remains" in result.message

    def test_a_404_stops_the_run(self, engine: WorkflowEngine, validator: PlanValidator) -> None:
        result = _run(
            engine,
            validator,
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-9999")]),
            role=Role.VIEWER,
        )
        assert result.status == "failed"
        assert result.state.results["s1"].status_code == 404
