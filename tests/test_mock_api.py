"""Phase 1 tests: the contract and the business rules.

Two things get proven here.

1. **The published contract is complete.** Every operation declares a risk level
   and a role list, and those declarations match the danger of the operation. If
   this drifts, the assistant's guardrails are reasoning about a lie.
2. **The rules actually hold.** A refund cannot exceed the balance, a void
   invoice cannot be refunded, a paying customer cannot be deleted. These are the
   failures the assistant is supposed to prevent — but the API must refuse them
   independently, because a guardrail you can bypass by calling the API directly
   is not a guardrail.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nl2api.mock_api.main import app
from nl2api.mock_api.risk import RiskLevel
from nl2api.mock_api.store import store

VIEWER = {"X-Role": "viewer"}
AGENT = {"X-Role": "support_agent"}
ADMIN = {"X-Role": "billing_admin"}


@pytest.fixture(autouse=True)
def _reset_store() -> Iterator[None]:
    """Every test starts from the same seed."""
    store.reset()
    yield
    store.reset()


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def openapi(client: TestClient) -> dict[str, Any]:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def _operations(schema: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (method, path, operation) for every operation in the document."""
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in {"get", "post", "patch", "put", "delete"}:
                yield method, path, operation


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class TestOpenAPIContract:
    def test_every_operation_declares_risk_and_roles(self, openapi: dict[str, Any]) -> None:
        missing = [
            f"{method.upper()} {path}"
            for method, path, op in _operations(openapi)
            if "x-risk" not in op or "x-required-roles" not in op
        ]
        assert missing == [], f"operations without declared risk: {missing}"

    def test_risk_levels_are_from_the_enum(self, openapi: dict[str, Any]) -> None:
        allowed = {level.value for level in RiskLevel}
        for method, path, op in _operations(openapi):
            assert op["x-risk"] in allowed, f"{method.upper()} {path} has risk {op['x-risk']!r}"

    def test_every_operation_has_an_operation_id_and_a_summary(
        self, openapi: dict[str, Any]
    ) -> None:
        """The planner addresses endpoints by operation_id, so they must be explicit."""
        seen: set[str] = set()
        for method, path, op in _operations(openapi):
            where = f"{method.upper()} {path}"
            assert op.get("operationId"), f"{where} has no operationId"
            assert op.get("summary"), f"{where} has no summary"
            assert op["operationId"] not in seen, f"duplicate operationId at {where}"
            seen.add(op["operationId"])

    def test_side_effects_are_written_for_a_human(self, openapi: dict[str, Any]) -> None:
        """The approval UI shows this text, so it must be a sentence."""
        for method, path, op in _operations(openapi):
            text = op["x-side-effects"]
            assert len(text) > 20, f"{method.upper()} {path} side effects too terse: {text!r}"
            assert text.endswith("."), f"{method.upper()} {path} side effects not a sentence"

    def test_get_operations_are_read_only_and_idempotent(self, openapi: dict[str, Any]) -> None:
        for method, path, op in _operations(openapi):
            if method == "get":
                assert op["x-risk"] == RiskLevel.READ_ONLY.value, f"GET {path} is not read-only"
                assert op["x-idempotent"] is True

    def test_read_only_operations_never_require_more_than_viewer(
        self, openapi: dict[str, Any]
    ) -> None:
        for method, path, op in _operations(openapi):
            if op["x-risk"] == RiskLevel.READ_ONLY.value:
                assert "viewer" in op["x-required-roles"], f"{method.upper()} {path} blocks viewers"

    @pytest.mark.parametrize(
        ("operation_id", "expected_risk", "least_privileged_role"),
        [
            ("search_customers", "read_only", "viewer"),
            ("get_invoice", "read_only", "viewer"),
            ("update_customer", "low_risk_write", "support_agent"),
            ("create_ticket", "low_risk_write", "support_agent"),
            ("add_ticket_comment", "low_risk_write", "support_agent"),
            ("delete_customer", "high_risk_write", "billing_admin"),
            ("create_refund", "high_risk_write", "billing_admin"),
            ("cancel_subscription", "high_risk_write", "billing_admin"),
            ("change_subscription_plan", "high_risk_write", "billing_admin"),
        ],
    )
    def test_specific_operations_are_classified_correctly(
        self,
        openapi: dict[str, Any],
        operation_id: str,
        expected_risk: str,
        least_privileged_role: str,
    ) -> None:
        """Pin the classification of the operations the demo depends on."""
        found = [op for _, _, op in _operations(openapi) if op["operationId"] == operation_id]
        assert found, f"{operation_id} is not in the schema"
        operation = found[0]
        assert operation["x-risk"] == expected_risk
        assert least_privileged_role in operation["x-required-roles"]

    def test_refunds_are_declared_non_idempotent(self, openapi: dict[str, Any]) -> None:
        """Retrying a refund must never look safe to the executor."""
        refund = next(
            op for _, _, op in _operations(openapi) if op["operationId"] == "create_refund"
        )
        assert refund["x-idempotent"] is False

    def test_role_hierarchy_is_published(self, openapi: dict[str, Any]) -> None:
        assert openapi["x-role-hierarchy"]["order"] == [
            "viewer",
            "support_agent",
            "billing_admin",
        ]

    def test_endpoint_count_is_what_the_plan_says(self, openapi: dict[str, Any]) -> None:
        """Pinned so a new endpoint cannot slip in without a deliberate edit here."""
        assert len(list(_operations(openapi))) == 17


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
class TestReads:
    def test_search_by_email_is_exact(self, client: TestClient) -> None:
        response = client.get("/customers", params={"email": "ana@acme.io"}, headers=VIEWER)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["data"][0]["id"] == "CUS-1001"

    def test_exact_duplicate_names_are_ambiguous(self, client: TestClient) -> None:
        """Two customers are both named 'Ana Ruiz'.

        Even a full-name search cannot disambiguate them. This is the case the
        assistant must resolve by asking, never by taking data[0].
        """
        response = client.get("/customers", params={"name": "Ana Ruiz"}, headers=VIEWER)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert {c["id"] for c in body["data"]} == {"CUS-1001", "CUS-1007"}

    def test_partial_name_matches_across_different_people(self, client: TestClient) -> None:
        """Substring matching is deliberately loose: 'ana' also matches 'Dana'.

        A planner that treats a name lookup as a unique key will get this wrong,
        which is exactly why the executor halts on multiple matches.
        """
        response = client.get("/customers", params={"name": "ana"}, headers=VIEWER)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert {c["id"] for c in body["data"]} == {"CUS-1001", "CUS-1002", "CUS-1007"}

    def test_search_with_no_match_returns_an_empty_page_not_404(self, client: TestClient) -> None:
        response = client.get("/customers", params={"email": "nobody@nowhere.io"}, headers=VIEWER)
        assert response.status_code == 200
        assert response.json() == {"data": [], "total": 0}

    def test_invoices_are_newest_first(self, client: TestClient) -> None:
        """'The last invoice' must be data[0], or every plan that says so is wrong."""
        response = client.get("/invoices", params={"customer_id": "CUS-1001"}, headers=VIEWER)
        assert response.status_code == 200
        issued = [row["issued_at"] for row in response.json()["data"]]
        assert issued == sorted(issued, reverse=True)

    def test_unknown_customer_returns_the_error_envelope(self, client: TestClient) -> None:
        response = client.get("/customers/CUS-9999", headers=VIEWER)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "customer_not_found"

    def test_invalid_status_filter_is_rejected(self, client: TestClient) -> None:
        response = client.get("/invoices", params={"status": "banana"}, headers=VIEWER)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class TestRoleEnforcement:
    def test_viewer_cannot_create_a_ticket(self, client: TestClient) -> None:
        response = client.post(
            "/tickets",
            json={"customer_id": "CUS-1001", "subject": "Hello there", "body": "Testing."},
            headers=VIEWER,
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "insufficient_role"

    def test_support_agent_cannot_issue_a_refund(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 100, "reason": "Test refund"},
            headers=AGENT,
        )
        assert response.status_code == 403
        assert store.list_refunds() == [], "a rejected call must not move money"

    def test_billing_admin_inherits_support_agent_permissions(self, client: TestClient) -> None:
        response = client.post(
            "/tickets",
            json={"customer_id": "CUS-1001", "subject": "Admin opened this", "body": "Body text."},
            headers=ADMIN,
        )
        assert response.status_code == 201

    def test_unknown_role_is_rejected(self, client: TestClient) -> None:
        response = client.get("/customers", headers={"X-Role": "superuser"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "unknown_role"


# ---------------------------------------------------------------------------
# Refunds — the money-moving surface
# ---------------------------------------------------------------------------
class TestRefunds:
    def test_full_refund_moves_the_invoice_to_refunded(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 24_000, "reason": "Duplicate charge"},
            headers=ADMIN | {"X-Actor": "ana.lead@support.io"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["amount_cents"] == 24_000
        assert body["customer_id"] == "CUS-1001"
        assert body["created_by"] == "ana.lead@support.io"

        invoice = client.get("/invoices/INV-1007", headers=VIEWER).json()
        assert invoice["status"] == "refunded"
        assert invoice["refunded_cents"] == 24_000

    def test_partial_refund_leaves_a_refundable_balance(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 10_000, "reason": "Goodwill"},
            headers=ADMIN,
        )
        assert response.status_code == 201

        invoice = client.get("/invoices/INV-1007", headers=VIEWER).json()
        assert invoice["status"] == "partially_refunded"
        assert invoice["amount_cents"] - invoice["refunded_cents"] == 14_000

    def test_refund_over_the_balance_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 24_001, "reason": "Too much"},
            headers=ADMIN,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "refund_exceeds_balance"
        assert store.get_invoice("INV-1007").refunded_cents == 0

    def test_second_refund_cannot_exceed_what_remains(self, client: TestClient) -> None:
        first = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 20_000, "reason": "Partial"},
            headers=ADMIN,
        )
        assert first.status_code == 201

        second = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 5_000, "reason": "The rest"},
            headers=ADMIN,
        )
        assert second.status_code == 422
        assert second.json()["error"]["code"] == "refund_exceeds_balance"
        assert store.get_invoice("INV-1007").refunded_cents == 20_000

    def test_open_invoice_cannot_be_refunded(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1003", "amount_cents": 100, "reason": "Never paid"},
            headers=ADMIN,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invoice_not_refundable"

    def test_void_invoice_cannot_be_refunded(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1005", "amount_cents": 100, "reason": "Voided"},
            headers=ADMIN,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invoice_not_refundable"

    def test_fully_refunded_invoice_rejects_further_refunds(self, client: TestClient) -> None:
        client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 24_000, "reason": "All of it"},
            headers=ADMIN,
        )
        again = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 1, "reason": "One more"},
            headers=ADMIN,
        )
        assert again.status_code == 422
        assert again.json()["error"]["code"] == "invoice_fully_refunded"

    def test_zero_amount_is_rejected_by_the_schema(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 0, "reason": "Nothing"},
            headers=ADMIN,
        )
        assert response.status_code == 422

    def test_unknown_invoice_is_a_404(self, client: TestClient) -> None:
        response = client.post(
            "/refunds",
            json={"invoice_id": "INV-9999", "amount_cents": 100, "reason": "Ghost"},
            headers=ADMIN,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invoice_not_found"

    def test_unknown_body_field_is_rejected(self, client: TestClient) -> None:
        """extra='forbid' stops the model from smuggling in fields we do not honour."""
        response = client.post(
            "/refunds",
            json={
                "invoice_id": "INV-1007",
                "amount_cents": 100,
                "reason": "Test",
                "skip_approval": True,
            },
            headers=ADMIN,
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
class TestSubscriptions:
    def test_change_plan_recalculates_mrr(self, client: TestClient) -> None:
        response = client.post(
            "/subscriptions/SUB-2001/change-plan",
            json={"plan": "starter", "seats": 12, "reason": "Downgrade requested"},
            headers=ADMIN,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["plan"] == "starter"
        assert body["mrr_cents"] == 2_900 * 12

    def test_no_op_plan_change_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/subscriptions/SUB-2001/change-plan",
            json={"plan": "pro", "seats": 12, "reason": "No actual change"},
            headers=ADMIN,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "no_change_requested"

    def test_enterprise_requires_ten_seats(self, client: TestClient) -> None:
        response = client.post(
            "/subscriptions/SUB-2007/change-plan",
            json={"plan": "enterprise", "seats": 4, "reason": "Upgrade"},
            headers=ADMIN,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "enterprise_seat_minimum"

    def test_cancel_marks_the_subscription_canceled(self, client: TestClient) -> None:
        response = client.post(
            "/subscriptions/SUB-2001/cancel",
            json={"reason": "Customer asked", "cancel_at": "immediate"},
            headers=ADMIN,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "canceled"
        assert body["renews_at"] is None

    def test_cancelling_twice_is_a_conflict(self, client: TestClient) -> None:
        response = client.post(
            "/subscriptions/SUB-2006/cancel",
            json={"reason": "Already gone"},
            headers=ADMIN,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "subscription_already_canceled"


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------
class TestTickets:
    def test_create_ticket_returns_an_open_ticket(self, client: TestClient) -> None:
        response = client.post(
            "/tickets",
            json={
                "customer_id": "CUS-1001",
                "subject": "Billing question",
                "body": "Why two charges this month?",
                "priority": "high",
            },
            headers=AGENT,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "open"
        assert body["priority"] == "high"
        assert body["id"].startswith("TIC-")

    def test_ticket_for_unknown_customer_is_a_404(self, client: TestClient) -> None:
        response = client.post(
            "/tickets",
            json={"customer_id": "CUS-9999", "subject": "Ghost", "body": "No such customer."},
            headers=AGENT,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "customer_not_found"

    def test_comment_is_appended_to_the_ticket(self, client: TestClient) -> None:
        response = client.post(
            "/tickets/TIC-3001/comments",
            json={
                "author": "sam@support.io",
                "body": "Refund is being reviewed.",
                "internal": True,
            },
            headers=AGENT,
        )
        assert response.status_code == 201
        comments = response.json()["comments"]
        assert len(comments) == 1
        assert comments[0]["internal"] is True

    def test_update_changes_status(self, client: TestClient) -> None:
        response = client.patch("/tickets/TIC-3001", json={"status": "resolved"}, headers=AGENT)
        assert response.status_code == 200
        assert response.json()["status"] == "resolved"

    def test_empty_update_is_rejected(self, client: TestClient) -> None:
        response = client.patch("/tickets/TIC-3001", json={}, headers=AGENT)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "empty_update"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
class TestCustomerDeletion:
    def test_deleting_a_paying_customer_is_blocked(self, client: TestClient) -> None:
        response = client.delete("/customers/CUS-1001", headers=ADMIN)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "customer_has_active_subscription"
        assert "CUS-1001" in store.customers

    def test_deleting_a_churned_customer_succeeds(self, client: TestClient) -> None:
        response = client.delete("/customers/CUS-1006", headers=ADMIN)
        assert response.status_code == 204
        assert "CUS-1006" not in store.customers

    def test_delete_requires_billing_admin(self, client: TestClient) -> None:
        response = client.delete("/customers/CUS-1006", headers=AGENT)
        assert response.status_code == 403
        assert "CUS-1006" in store.customers


# ---------------------------------------------------------------------------
# Store invariants
# ---------------------------------------------------------------------------
class TestStoreIsolation:
    def test_reset_restores_the_seed(self, client: TestClient) -> None:
        before = store.snapshot()
        client.post(
            "/refunds",
            json={"invoice_id": "INV-1007", "amount_cents": 24_000, "reason": "Mutate"},
            headers=ADMIN,
        )
        assert store.snapshot() != before
        store.reset()
        assert store.snapshot() == before

    def test_seed_is_stable_across_resets(self) -> None:
        first = store.snapshot()
        store.reset()
        assert store.snapshot() == first
