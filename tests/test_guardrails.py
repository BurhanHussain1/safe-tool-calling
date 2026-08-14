"""Phase 3 tests: the layer that decides whether a plan may run.

This is the part of the system whose failures are expensive, so the tests are
adversarial rather than confirmatory. They mostly ask "what does it do when the
model is wrong, or lying, or has been talked into something".
"""

from __future__ import annotations

from typing import Any

import pytest

from nl2api.config import Role, Settings
from nl2api.guardrails.dryrun import build_previews
from nl2api.guardrails.policy import Decision, PolicyEngine
from nl2api.guardrails.redaction import REDACTED, mask_email, redact, redact_text
from nl2api.guardrails.validator import PlanValidator, coerce, target_type
from nl2api.mock_api.main import build_openapi, create_app
from nl2api.mock_api.store import Store
from nl2api.planner.models import Argument, Plan, PlanStep
from nl2api.schema.registry import ToolRegistry


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry.from_openapi(build_openapi(create_app()))


@pytest.fixture(scope="module")
def validator(registry: ToolRegistry) -> PlanValidator:
    return PlanValidator(registry)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def engine(registry: ToolRegistry, settings: Settings) -> PolicyEngine:
    return PolicyEngine(registry, settings)


@pytest.fixture
def store() -> Store:
    return Store()


class StoreReader:
    """A read-only view of the datastore, for dry-run previews.

    Deliberately exposes nothing but ``read``: the preview builder is handed an
    object that has no way to change anything.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def read(self, resource: str, identifier: str) -> dict[str, Any] | None:
        table = {
            "customer": self._store.customers,
            "invoice": self._store.invoices,
            "subscription": self._store.subscriptions,
            "ticket": self._store.tickets,
        }.get(resource, {})
        record = table.get(identifier)
        return record.model_dump(mode="json") if record else None


def _plan(*steps: PlanStep) -> Plan:
    return Plan(intent="test", steps=list(steps))


def _step(
    step_id: str,
    operation_id: str,
    arguments: list[tuple[str, str, str]] | None = None,
) -> PlanStep:
    return PlanStep(
        id=step_id,
        operation_id=operation_id,
        arguments=[
            Argument(name=n, location=loc, value=v)  # type: ignore[arg-type]
            for n, loc, v in (arguments or [])
        ],
        reason="Because.",
        expected_result="A thing.",
    )


def _refund_step(amount: str = "24000", invoice: str = "INV-1007") -> PlanStep:
    return _step(
        "s1",
        "create_refund",
        [
            ("invoice_id", "body", invoice),
            ("amount_cents", "body", amount),
            ("reason", "body", "Duplicate charge"),
        ],
    )


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------
class TestCoercion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("24000", 24000), ("0", 0), ("-5", -5), ("  42  ", 42)],
    )
    def test_valid_integers(self, text: str, expected: int) -> None:
        value, error = coerce(text, {"type": "integer"})
        assert error is None
        assert value == expected

    @pytest.mark.parametrize("text", ["24,000", "$240", "24000.5", "24 000", "lots", "1e5", ""])
    def test_lenient_integer_forms_are_rejected(self, text: str) -> None:
        """A parser that accepts '$240' is how the wrong refund gets issued."""
        value, error = coerce(text, {"type": "integer"})
        assert value is None
        assert error is not None

    @pytest.mark.parametrize(("text", "expected"), [("true", True), ("FALSE", False), ("1", True)])
    def test_booleans(self, text: str, expected: bool) -> None:
        value, error = coerce(text, {"type": "boolean"})
        assert error is None
        assert value is expected

    def test_ambiguous_boolean_is_rejected(self) -> None:
        _, error = coerce("maybe", {"type": "boolean"})
        assert error is not None

    def test_strings_pass_through_untouched(self) -> None:
        value, error = coerce("  spaces preserved  ", {"type": "string"})
        assert error is None
        assert value == "  spaces preserved  "

    def test_optional_type_sees_through_the_null_branch(self) -> None:
        schema = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        assert target_type(schema) == "integer"
        assert coerce("7", schema) == (7, None)

    def test_genuine_union_refuses_to_guess(self) -> None:
        schema = {"anyOf": [{"type": "integer"}, {"type": "string"}]}
        assert target_type(schema) is None

    def test_structured_types_are_refused(self) -> None:
        _, error = coerce("a,b,c", {"type": "array"})
        assert error is not None and "array" in error


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidator:
    def test_a_correct_plan_validates(self, validator: PlanValidator) -> None:
        report = validator.validate(_plan(_refund_step()))
        assert report.ok, report.messages
        call = report.call("s1")
        assert call is not None
        assert call.body == {
            "invoice_id": "INV-1007",
            "amount_cents": 24000,
            "reason": "Duplicate charge",
        }

    def test_amount_is_coerced_to_an_integer(self, validator: PlanValidator) -> None:
        call = validator.validate(_plan(_refund_step())).call("s1")
        assert call is not None
        assert isinstance(call.body["amount_cents"], int)  # type: ignore[index]

    def test_bad_amount_is_rejected_with_the_field_named(self, validator: PlanValidator) -> None:
        report = validator.validate(_plan(_refund_step(amount="$240.00")))
        assert not report.ok
        assert report.errors[0].name == "amount_cents"
        assert report.errors[0].code == "type_mismatch"

    def test_unknown_operation_is_rejected(self, validator: PlanValidator) -> None:
        report = validator.validate(_plan(_step("s1", "wire_money_to_me")))
        assert not report.ok
        assert report.errors[0].code == "unknown_operation"

    def test_unknown_body_field_is_rejected(self, validator: PlanValidator) -> None:
        """The model must not be able to smuggle in a field we might honour."""
        step = _step(
            "s1",
            "create_refund",
            [
                ("invoice_id", "body", "INV-1007"),
                ("amount_cents", "body", "100"),
                ("reason", "body", "Test"),
                ("skip_approval", "body", "true"),
            ],
        )
        report = validator.validate(_plan(step))
        assert not report.ok
        assert any(e.code == "unknown_field" and e.name == "skip_approval" for e in report.errors)

    def test_unknown_query_parameter_is_rejected(self, validator: PlanValidator) -> None:
        report = validator.validate(
            _plan(_step("s1", "list_invoices", [("limit", "query", "100")]))
        )
        assert not report.ok
        assert report.errors[0].code == "unknown_field"

    def test_missing_required_path_parameter_is_named(self, validator: PlanValidator) -> None:
        report = validator.validate(_plan(_step("s1", "get_invoice")))
        assert not report.ok
        error = report.errors[0]
        assert error.code == "missing_required"
        assert error.name == "invoice_id"

    def test_missing_required_body_field_is_named(self, validator: PlanValidator) -> None:
        step = _step("s1", "create_refund", [("invoice_id", "body", "INV-1007")])
        report = validator.validate(_plan(step))
        missing = {e.name for e in report.errors if e.code == "missing_required"}
        assert missing == {"amount_cents", "reason"}

    def test_parameter_in_the_wrong_location_is_rejected(self, validator: PlanValidator) -> None:
        """invoice_id is a path parameter on get_invoice, not a query one."""
        report = validator.validate(
            _plan(_step("s1", "get_invoice", [("invoice_id", "query", "INV-1007")]))
        )
        assert not report.ok
        assert any(e.code == "unknown_field" for e in report.errors)

    def test_enum_violation_is_caught(self, validator: PlanValidator) -> None:
        report = validator.validate(
            _plan(_step("s1", "list_invoices", [("status", "query", "banana")]))
        )
        assert not report.ok
        assert report.errors[0].code == "constraint_violation"

    def test_schema_constraint_is_enforced(self, validator: PlanValidator) -> None:
        """amount_cents has exclusiveMinimum 0, so zero must fail."""
        report = validator.validate(_plan(_refund_step(amount="0")))
        assert not report.ok
        assert any(e.code == "constraint_violation" for e in report.errors)

    def test_body_on_an_endpoint_that_takes_none_is_rejected(
        self, validator: PlanValidator
    ) -> None:
        report = validator.validate(
            _plan(
                _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007"), ("x", "body", "1")])
            )
        )
        assert not report.ok

    def test_a_question_has_nothing_to_validate(self, validator: PlanValidator) -> None:
        """'The model correctly declined' must not read as a validation failure."""
        report = validator.validate(Plan.asking("x", "Which invoice?"))
        assert report.ok
        assert report.calls == ()

    def test_a_refusal_has_nothing_to_validate(self, validator: PlanValidator) -> None:
        assert validator.validate(Plan.refusing("x", "No.")).ok

    def test_errors_from_several_steps_are_all_reported(self, validator: PlanValidator) -> None:
        """One round trip should surface every problem, not just the first."""
        report = validator.validate(_plan(_step("s1", "get_invoice"), _step("s2", "get_customer")))
        assert {e.step_id for e in report.errors} == {"s1", "s2"}


class TestDeferredValues:
    """Values that only exist once an earlier step has run."""

    def test_a_reference_is_recorded_rather_than_coerced(self, validator: PlanValidator) -> None:
        plan = _plan(
            _step("s1", "search_customers", [("email", "query", "ana@acme.io")]),
            _step("s2", "list_invoices", [("customer_id", "query", "$steps.s1.data[0].id")]),
        )
        report = validator.validate(plan)
        assert report.ok, report.messages

        call = report.call("s2")
        assert call is not None
        assert call.has_deferred_values
        assert call.deferred[0].name == "customer_id"
        assert "customer_id" not in call.query_params

    def test_a_deferred_value_satisfies_a_required_field(self, validator: PlanValidator) -> None:
        """A required field supplied by reference must not read as missing."""
        plan = _plan(
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "$steps.s1.id"),
                    ("amount_cents", "body", "$steps.s1.amount_cents"),
                    ("reason", "body", "Duplicate charge"),
                ],
            ),
        )
        report = validator.validate(plan)
        assert report.ok, report.messages

    def test_url_path_refuses_to_build_with_an_unresolved_parameter(
        self, validator: PlanValidator
    ) -> None:
        plan = _plan(
            _step("s1", "search_customers", [("email", "query", "ana@acme.io")]),
            _step("s2", "get_customer", [("customer_id", "path", "$steps.s1.data[0].id")]),
        )
        call = validator.validate(plan).call("s2")
        assert call is not None
        with pytest.raises(ValueError, match="unresolved"):
            call.url_path()

    def test_url_path_builds_when_everything_is_known(self, validator: PlanValidator) -> None:
        call = validator.validate(
            _plan(_step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]))
        ).call("s1")
        assert call is not None
        assert call.url_path() == "/invoices/INV-1007"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
class TestPolicy:
    def _evaluate(self, validator: PlanValidator, engine: PolicyEngine, plan: Plan, role: Role):
        report = validator.validate(plan)
        assert report.ok, report.messages
        return engine.evaluate(report.calls, role)

    def test_reads_are_allowed_outright(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        plan = _plan(_step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]))
        result = self._evaluate(validator, engine, plan, Role.VIEWER)
        assert result.outcome is Decision.ALLOW
        assert result.may_execute_now

    def test_low_risk_writes_run_but_are_reported(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        plan = _plan(
            _step(
                "s1",
                "create_ticket",
                [
                    ("customer_id", "body", "CUS-1001"),
                    ("subject", "body", "Billing question"),
                    ("body", "body", "Charged twice."),
                ],
            )
        )
        result = self._evaluate(validator, engine, plan, Role.SUPPORT_AGENT)
        assert result.outcome is Decision.ALLOW_WITH_NOTICE
        assert result.may_execute_now
        assert result.reasons

    def test_high_risk_writes_are_held_for_approval(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        result = self._evaluate(validator, engine, _plan(_refund_step()), Role.BILLING_ADMIN)
        assert result.outcome is Decision.REQUIRE_APPROVAL
        assert not result.may_execute_now
        assert len(result.awaiting_approval) == 1

    def test_insufficient_role_is_denied_before_anything_else(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        result = self._evaluate(validator, engine, _plan(_refund_step()), Role.SUPPORT_AGENT)
        assert result.outcome is Decision.DENY
        assert "billing_admin" in " ".join(result.reasons)

    def test_viewer_cannot_open_a_ticket(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        plan = _plan(
            _step(
                "s1",
                "create_ticket",
                [
                    ("customer_id", "body", "CUS-1001"),
                    ("subject", "body", "Hello there"),
                    ("body", "body", "Testing."),
                ],
            )
        )
        result = self._evaluate(validator, engine, plan, Role.VIEWER)
        assert result.outcome is Decision.DENY

    def test_the_strictest_step_decides_the_plan(
        self, validator: PlanValidator, engine: PolicyEngine
    ) -> None:
        plan = Plan(
            intent="mixed",
            steps=[
                _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
                PlanStep(
                    id="s2",
                    operation_id="create_refund",
                    arguments=[
                        Argument(name="invoice_id", location="body", value="INV-1007"),
                        Argument(name="amount_cents", location="body", value="100"),
                        Argument(name="reason", location="body", value="Goodwill"),
                    ],
                    reason="r",
                    expected_result="e",
                ),
            ],
        )
        result = self._evaluate(validator, engine, plan, Role.BILLING_ADMIN)
        assert result.decision_for("s1").decision is Decision.ALLOW  # type: ignore[union-attr]
        assert result.outcome is Decision.REQUIRE_APPROVAL


class TestValueCeiling:
    def test_a_refund_at_the_ceiling_is_refused_not_gated(
        self, validator: PlanValidator, registry: ToolRegistry
    ) -> None:
        engine = PolicyEngine(registry, Settings(refund_max_cents=50_000, _env_file=None))  # type: ignore[call-arg]
        report = validator.validate(_plan(_refund_step(amount="50000", invoice="INV-1002")))
        result = engine.evaluate(report.calls, Role.BILLING_ADMIN)

        assert result.outcome is Decision.DENY, "at the ceiling must not merely require approval"
        assert "ceiling" in " ".join(result.reasons)

    def test_just_below_the_ceiling_is_gated_as_normal(
        self, validator: PlanValidator, registry: ToolRegistry
    ) -> None:
        engine = PolicyEngine(registry, Settings(refund_max_cents=50_000, _env_file=None))  # type: ignore[call-arg]
        report = validator.validate(_plan(_refund_step(amount="49999", invoice="INV-1002")))
        result = engine.evaluate(report.calls, Role.BILLING_ADMIN)
        assert result.outcome is Decision.REQUIRE_APPROVAL

    def test_a_deferred_amount_cannot_be_checked_yet(
        self, validator: PlanValidator, registry: ToolRegistry
    ) -> None:
        """Honest about its limits: the number does not exist until s1 runs.

        The check runs again after resolution, before the call goes out.
        """
        engine = PolicyEngine(registry, Settings(refund_max_cents=1, _env_file=None))  # type: ignore[call-arg]
        plan = _plan(
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1002")]),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "INV-1002"),
                    ("amount_cents", "body", "$steps.s1.amount_cents"),
                    ("reason", "body", "Duplicate"),
                ],
            ),
        )
        report = validator.validate(plan)
        result = engine.evaluate(report.calls, Role.BILLING_ADMIN)
        assert result.outcome is Decision.REQUIRE_APPROVAL


class TestBlastRadius:
    def test_too_many_writes_denies_the_writes(
        self, validator: PlanValidator, registry: ToolRegistry
    ) -> None:
        engine = PolicyEngine(registry, Settings(max_write_steps=2, _env_file=None))  # type: ignore[call-arg]
        steps = [
            _step(
                f"s{i}",
                "create_ticket",
                [
                    ("customer_id", "body", "CUS-1001"),
                    ("subject", "body", f"Ticket number {i}"),
                    ("body", "body", "Body text."),
                ],
            )
            for i in range(1, 4)
        ]
        report = validator.validate(Plan(intent="many", steps=steps))
        assert report.ok, report.messages

        result = engine.evaluate(report.calls, Role.SUPPORT_AGENT)
        assert result.outcome is Decision.DENY
        assert len(result.denied) == 3
        assert "limit is 2" in " ".join(result.plan_reasons)

    def test_many_reads_are_fine(self, validator: PlanValidator, registry: ToolRegistry) -> None:
        """The cap counts writes, not steps — a long lookup chain is harmless."""
        engine = PolicyEngine(registry, Settings(max_write_steps=1, _env_file=None))  # type: ignore[call-arg]
        steps = [
            _step(f"s{i}", "get_invoice", [("invoice_id", "path", f"INV-100{i}")])
            for i in range(1, 5)
        ]
        report = validator.validate(Plan(intent="reads", steps=steps))
        result = engine.evaluate(report.calls, Role.VIEWER)
        assert result.outcome is Decision.ALLOW


# ---------------------------------------------------------------------------
# Dry runs
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_previewing_mutates_nothing(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        """The property the whole approval step rests on."""
        before = store.snapshot()

        plan = Plan(
            intent="everything dangerous at once",
            steps=[
                _refund_step(),
                _step("s2", "delete_customer", [("customer_id", "path", "CUS-1006")]),
                _step(
                    "s3",
                    "cancel_subscription",
                    [("subscription_id", "path", "SUB-2001"), ("reason", "body", "Asked to")],
                ),
            ],
        )
        report = validator.validate(plan)
        assert report.ok, report.messages

        previews = build_previews(report.calls, registry, StoreReader(store))
        assert len(previews) == 3
        assert store.snapshot() == before, "a dry run changed the datastore"

    def test_reads_are_not_previewed(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(
            _plan(_step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]))
        )
        assert build_previews(report.calls, registry, StoreReader(store)) == ()

    def test_refund_preview_states_the_before_and_after(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(_plan(_refund_step(amount="24000")))
        preview = build_previews(report.calls, registry, StoreReader(store))[0]

        assert "$240.00" in preview.summary
        assert "INV-1007" in preview.summary
        assert preview.reversible is False
        assert preview.before == {
            "status": "paid",
            "refunded_cents": 0,
            "refundable_cents": 24000,
        }
        assert preview.after["status"] == "refunded"  # type: ignore[index]
        assert ("status", "paid", "refunded") in preview.changes

    def test_partial_refund_preview_shows_the_remaining_balance(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(_plan(_refund_step(amount="10000")))
        preview = build_previews(report.calls, registry, StoreReader(store))[0]
        assert preview.after["status"] == "partially_refunded"  # type: ignore[index]
        assert preview.after["refundable_cents"] == 14000  # type: ignore[index]

    def test_overlarge_refund_is_warned_about_before_the_api_rejects_it(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(_plan(_refund_step(amount="99999")))
        preview = build_previews(report.calls, registry, StoreReader(store))[0]
        assert any("will reject" in w for w in preview.warnings)

    def test_refund_preview_always_warns_about_repetition(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        """create_refund is declared non-idempotent; the approver must be told."""
        report = validator.validate(_plan(_refund_step()))
        preview = build_previews(report.calls, registry, StoreReader(store))[0]
        assert any("second refund" in w for w in preview.warnings)

    def test_delete_preview_is_blunt(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(
            _plan(_step("s1", "delete_customer", [("customer_id", "path", "CUS-1006")]))
        )
        preview = build_previews(report.calls, registry, StoreReader(store))[0]
        assert preview.reversible is False
        assert "Sara Nowak" in preview.summary
        assert any("no undo" in w for w in preview.warnings)

    def test_customer_visible_comment_is_flagged(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(
            _plan(
                _step(
                    "s1",
                    "add_ticket_comment",
                    [
                        ("ticket_id", "path", "TIC-3001"),
                        ("author", "body", "sam@support.io"),
                        ("body", "body", "We are looking into it."),
                        ("internal", "body", "false"),
                    ],
                )
            )
        )
        preview = build_previews(report.calls, registry, StoreReader(store))[0]
        assert "customer will see" in preview.summary
        assert any("cannot be unsent" in w for w in preview.warnings)

    def test_preview_works_without_a_state_reader(
        self, validator: PlanValidator, registry: ToolRegistry
    ) -> None:
        """A briefly unreachable API must not push people into approving blind."""
        report = validator.validate(_plan(_refund_step()))
        preview = build_previews(report.calls, registry, reader=None)[0]
        assert preview.summary
        assert preview.reversible is False
        assert preview.before is None

    def test_deferred_values_are_declared_not_printed_raw(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        plan = _plan(
            _step("s1", "get_invoice", [("invoice_id", "path", "INV-1007")]),
            _step(
                "s2",
                "create_refund",
                [
                    ("invoice_id", "body", "$steps.s1.id"),
                    ("amount_cents", "body", "$steps.s1.amount_cents"),
                    ("reason", "body", "Duplicate"),
                ],
            ),
        )
        preview = build_previews(validator.validate(plan).calls, registry, StoreReader(store))[0]
        assert "$steps." not in preview.summary
        assert "from an earlier step" in preview.summary
        assert any("not known yet" in w for w in preview.warnings)

    def test_render_is_readable(
        self, validator: PlanValidator, registry: ToolRegistry, store: Store
    ) -> None:
        report = validator.validate(_plan(_refund_step()))
        rendered = build_previews(report.calls, registry, StoreReader(store))[0].render()
        assert "$240.00" in rendered
        assert "status: paid → refunded" in rendered
        assert "cannot be undone" in rendered


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_email_keeps_enough_to_correlate(self) -> None:
        masked = mask_email("ana@acme.io")
        assert masked.startswith("a")
        assert masked.endswith("@acme.io")
        assert "ana@" not in masked

    def test_secret_named_fields_are_erased_entirely(self) -> None:
        payload = {
            "authorization_token": "ghp_abcdefghijklmnopqrstuvwxyz",
            "api_key": "sk-livesecret",
            "customer_id": "CUS-1001",
        }
        result = redact(payload)
        assert result["authorization_token"] == REDACTED
        assert result["api_key"] == REDACTED
        assert result["customer_id"] == "CUS-1001", "identifiers stay legible"

    def test_inline_credentials_in_free_text_are_erased(self) -> None:
        text = "call it with Bearer abc123DEF456ghi789 please"
        assert "abc123DEF456ghi789" not in redact_text(text)

    def test_nested_structures_are_walked(self) -> None:
        payload = {"steps": [{"body": {"email": "dana@northwind.co", "token": "sk-abcdefgh"}}]}
        result = redact(payload)
        assert result["steps"][0]["body"]["token"] == REDACTED
        assert result["steps"][0]["body"]["email"] == "d***@northwind.co"

    def test_redaction_does_not_mutate_the_caller_copy(self) -> None:
        """The executor is still using this dict."""
        original = {"email": "ana@acme.io", "nested": {"token": "sk-abcdefgh"}}
        redact(original)
        assert original["email"] == "ana@acme.io"
        assert original["nested"]["token"] == "sk-abcdefgh"

    def test_non_strings_survive(self) -> None:
        assert redact({"amount_cents": 24000, "ok": True, "none": None}) == {
            "amount_cents": 24000,
            "ok": True,
            "none": None,
        }
