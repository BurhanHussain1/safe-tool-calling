"""Phase 2 tests: the tool catalogue and retrieval.

The catalogue is what the guardrails reason against. If it misreports risk,
drops a required parameter, or leaves a `$ref` unresolved, every layer above it
is validating against a document that does not describe the API.
"""

from __future__ import annotations

from typing import Any

import pytest

from nl2api.config import Role
from nl2api.mock_api.main import build_openapi, create_app
from nl2api.mock_api.risk import RiskLevel
from nl2api.schema.parser import OpenAPIParseError, parse_openapi, resolve_refs
from nl2api.schema.registry import ToolRegistry, UnknownOperation
from nl2api.schema.retriever import BM25Retriever, expand_query, tokenize


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return build_openapi(create_app())


@pytest.fixture(scope="module")
def registry(document: dict[str, Any]) -> ToolRegistry:
    return ToolRegistry.from_openapi(document)


@pytest.fixture(scope="module")
def retriever(registry: ToolRegistry) -> BM25Retriever:
    return BM25Retriever(registry)


def _index_of(retriever: BM25Retriever, operation_id: str) -> int:
    """Position of an operation in the retriever's internal document list."""
    return next(
        i for i, spec in enumerate(retriever.registry.all()) if spec.operation_id == operation_id
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestParser:
    def test_every_endpoint_becomes_a_tool(self, registry: ToolRegistry) -> None:
        assert len(registry) == 17

    def test_risk_and_roles_survive_the_round_trip(self, registry: ToolRegistry) -> None:
        refund = registry.get("create_refund")
        assert refund.risk is RiskLevel.HIGH_RISK_WRITE
        assert refund.minimum_role is Role.BILLING_ADMIN
        assert refund.needs_approval is True
        assert refund.idempotent is False
        assert refund.side_effects.endswith(".")

    def test_read_only_tools_are_not_writes(self, registry: ToolRegistry) -> None:
        assert len(registry.reads()) + len(registry.writes()) == len(registry)
        for spec in registry.reads():
            assert spec.risk is RiskLevel.READ_ONLY
            assert not spec.needs_approval

    def test_path_parameters_are_required(self, registry: ToolRegistry) -> None:
        spec = registry.get("get_invoice")
        names = {p.name for p in spec.path_parameters}
        assert names == {"invoice_id"}
        assert spec.parameter("invoice_id").required is True  # type: ignore[union-attr]

    def test_optional_query_parameters_are_optional(self, registry: ToolRegistry) -> None:
        spec = registry.get("list_invoices")
        customer = spec.parameter("customer_id")
        assert customer is not None
        assert customer.location == "query"
        assert customer.required is False

    def test_enum_values_are_extracted_from_optional_parameters(
        self, registry: ToolRegistry
    ) -> None:
        """`status` is `Literal[...] | None`, so the enum hides inside an anyOf."""
        status = registry.get("list_invoices").parameter("status")
        assert status is not None
        assert "paid" in status.enum_values
        assert "void" in status.enum_values
        assert "null" not in status.enum_values

    def test_header_parameters_are_not_offered_to_the_model(self, registry: ToolRegistry) -> None:
        """X-Role is the executor's to set, so it must never appear as plannable."""
        for spec in registry:
            assert all(p.location in {"path", "query"} for p in spec.parameters)
            assert not any(p.name.lower().startswith("x-") for p in spec.parameters)

    def test_request_body_refs_are_inlined(self, registry: ToolRegistry) -> None:
        body = registry.get("create_refund").request_body_schema
        assert body is not None
        assert "$ref" not in str(body), "schema still contains an unresolved reference"
        assert set(body["properties"]) == {"invoice_id", "amount_cents", "reason"}
        assert set(body["required"]) == {"invoice_id", "amount_cents", "reason"}

    def test_response_refs_are_inlined(self, registry: ToolRegistry) -> None:
        response = registry.get("list_invoices").response_schema
        assert response is not None
        assert "$ref" not in str(response)
        # Page[Invoice] nests the item schema under data.items
        assert "amount_cents" in response["properties"]["data"]["items"]["properties"]

    def test_body_property_names_and_required_names(self, registry: ToolRegistry) -> None:
        spec = registry.get("create_ticket")
        assert "priority" in spec.body_property_names
        assert set(spec.required_body_property_names) == {"customer_id", "subject", "body"}

    def test_document_without_paths_is_rejected(self) -> None:
        with pytest.raises(OpenAPIParseError, match="no 'paths'"):
            parse_openapi({"openapi": "3.1.0"})

    def test_operations_without_an_operation_id_are_skipped(self) -> None:
        """Unreachable by design: the planner addresses endpoints by id."""
        specs = parse_openapi({"paths": {"/thing": {"get": {"summary": "No id", "responses": {}}}}})
        assert specs == []


class TestFailClosed:
    """A missing or broken risk declaration must make an endpoint harder to call."""

    def test_missing_risk_is_treated_as_the_most_dangerous(self) -> None:
        specs = parse_openapi(
            {
                "paths": {
                    "/danger": {
                        "post": {"operationId": "undeclared", "summary": "No risk", "responses": {}}
                    }
                }
            }
        )
        assert specs[0].risk is RiskLevel.HIGH_RISK_WRITE
        assert specs[0].minimum_role is Role.BILLING_ADMIN
        assert specs[0].needs_approval is True

    def test_unrecognised_risk_value_fails_closed(self) -> None:
        specs = parse_openapi(
            {
                "paths": {
                    "/danger": {
                        "post": {
                            "operationId": "weird",
                            "x-risk": "totally_safe_trust_me",
                            "x-required-roles": ["viewer"],
                            "responses": {},
                        }
                    }
                }
            }
        )
        assert specs[0].risk is RiskLevel.HIGH_RISK_WRITE
        assert specs[0].minimum_role is Role.BILLING_ADMIN

    def test_unknown_role_names_are_dropped_and_the_rest_kept(self) -> None:
        specs = parse_openapi(
            {
                "paths": {
                    "/x": {
                        "get": {
                            "operationId": "mixed",
                            "x-risk": "read_only",
                            "x-required-roles": ["viewer", "root"],
                            "responses": {},
                        }
                    }
                }
            }
        )
        assert specs[0].required_roles == (Role.VIEWER,)

    def test_empty_role_list_restricts_to_billing_admin(self) -> None:
        specs = parse_openapi(
            {
                "paths": {
                    "/x": {
                        "get": {
                            "operationId": "roleless",
                            "x-risk": "read_only",
                            "x-required-roles": [],
                            "responses": {},
                        }
                    }
                }
            }
        )
        assert specs[0].minimum_role is Role.BILLING_ADMIN


class TestRefResolution:
    def test_sibling_keys_override_the_target(self) -> None:
        resolved = resolve_refs(
            {"$ref": "#/components/schemas/Thing", "description": "override"},
            {"Thing": {"type": "object", "description": "original"}},
        )
        assert resolved == {"type": "object", "description": "override"}

    def test_recursive_reference_terminates(self) -> None:
        """A self-referential schema must not hang the parser."""
        components = {"Node": {"type": "object", "properties": {"child": {"$ref": "#/c/Node"}}}}
        resolved = resolve_refs({"$ref": "#/c/Node"}, components)
        assert resolved["properties"]["child"]["type"] == "object"

    def test_unresolvable_reference_degrades_to_permissive(self) -> None:
        assert resolve_refs({"$ref": "#/components/schemas/Ghost"}, {}) == {}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_unknown_operation_suggests_a_near_miss(self, registry: ToolRegistry) -> None:
        with pytest.raises(UnknownOperation) as exc:
            registry.get("create_refunds")
        assert "create_refund" in str(exc.value)

    def test_duplicate_operation_ids_are_rejected(self, registry: ToolRegistry) -> None:
        spec = registry.get("get_invoice")
        with pytest.raises(ValueError, match="Duplicate operation id"):
            ToolRegistry([spec, spec])

    def test_callable_by_narrows_with_privilege(self, registry: ToolRegistry) -> None:
        viewer = {s.operation_id for s in registry.callable_by(Role.VIEWER)}
        agent = {s.operation_id for s in registry.callable_by(Role.SUPPORT_AGENT)}
        admin = {s.operation_id for s in registry.callable_by(Role.BILLING_ADMIN)}

        assert viewer < agent < admin
        assert "create_refund" not in agent
        assert "create_refund" in admin
        assert all(not s.is_write for s in registry.callable_by(Role.VIEWER))

    def test_requiring_approval_is_the_high_risk_set(self, registry: ToolRegistry) -> None:
        gated = {s.operation_id for s in registry.requiring_approval()}
        assert gated == {
            "cancel_subscription",
            "change_subscription_plan",
            "create_refund",
            "delete_customer",
        }

    def test_render_for_prompt_is_valid_json_with_what_the_model_needs(
        self, registry: ToolRegistry
    ) -> None:
        import json

        rendered = json.loads(registry.render_for_prompt(["create_refund", "get_invoice"]))
        assert [t["operation_id"] for t in rendered] == ["create_refund", "get_invoice"]

        refund = rendered[0]
        assert refund["method"] == "POST"
        assert refund["risk"] == "high_risk_write"
        assert refund["minimum_role"] == "billing_admin"
        assert refund["side_effects"]
        field_names = {f["name"] for f in refund["body"]["fields"]}
        assert field_names == {"invoice_id", "amount_cents", "reason"}
        assert all(f["required"] for f in refund["body"]["fields"])

    def test_rendered_reads_omit_side_effects(self, registry: ToolRegistry) -> None:
        import json

        rendered = json.loads(registry.render_for_prompt(["get_invoice"]))[0]
        assert "side_effects" not in rendered

    def test_rendered_enums_are_exposed_as_allowed_values(self, registry: ToolRegistry) -> None:
        import json

        rendered = json.loads(registry.render_for_prompt(["list_invoices"]))[0]
        status = next(p for p in rendered["parameters"] if p["name"] == "status")
        assert "paid" in status["allowed_values"]


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
class TestRetriever:
    def test_tokenizer_drops_stopwords_folds_plurals_and_keeps_content(self) -> None:
        assert tokenize("Show me the invoices for a customer") == [
            "show",
            "invoice",
            "customer",
        ]

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("refunds", "refund"),
            ("invoices", "invoice"),
            ("customers", "customer"),
            ("tickets", "ticket"),
            ("subscriptions", "subscription"),
            # Must not be mangled:
            ("status", "status"),
            ("address", "address"),
            ("this", "this"),
            ("its", "its"),
        ],
    )
    def test_plural_folding_is_conservative(self, word: str, expected: str) -> None:
        from nl2api.schema.retriever import singularize

        assert singularize(word) == expected

    def test_singular_query_matches_a_plural_endpoint(self, retriever: BM25Retriever) -> None:
        """'refund' must reach list_refunds, which is indexed as 'refunds'."""
        top = [c.operation_id for c in retriever.top_k("show the refund history", k=4)]
        assert "list_refunds" in top

    def test_query_expansion_adds_schema_vocabulary(self) -> None:
        expanded = expand_query("give the customer their money back")
        assert "refund" in expanded
        assert "money back" in expanded, "expansion must add, never replace"

    def test_expansion_leaves_unrelated_queries_alone(self) -> None:
        assert expand_query("list tickets") == "list tickets"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("refund the last invoice for ana@acme.io", "create_refund"),
            ("give them their money back", "create_refund"),
            ("what invoices are unpaid", "list_invoices"),
            ("open a support ticket about billing", "create_ticket"),
            ("cancel the subscription", "cancel_subscription"),
            ("look up the customer by email", "search_customers"),
            ("add an internal note to the ticket", "add_ticket_comment"),
            ("downgrade them to the starter plan", "change_subscription_plan"),
        ],
    )
    def test_expected_operation_is_shortlisted(
        self, retriever: BM25Retriever, query: str, expected: str
    ) -> None:
        top = [c.operation_id for c in retriever.top_k(query, k=4)]
        assert expected in top, f"{expected} missing from {top}\n{retriever.explain(query)}"

    def test_refund_ranks_first_for_an_obvious_refund_request(
        self, retriever: BM25Retriever
    ) -> None:
        top = retriever.top_k("issue a refund against invoice INV-1007", k=3)
        assert top[0].operation_id == "create_refund"

    def test_zero_scoring_operations_are_never_offered(self, retriever: BM25Retriever) -> None:
        """Offering an unrelated endpoint is an invitation to misuse it."""
        results = retriever.top_k("xyzzy plugh quuxbar", k=6)
        assert results == []

    def test_top_k_is_bounded(self, retriever: BM25Retriever) -> None:
        assert len(retriever.top_k("customer invoice ticket refund subscription", k=3)) == 3

    def test_results_are_ordered_by_descending_score(self, retriever: BM25Retriever) -> None:
        scores = [c.score for c in retriever.top_k("refund invoice customer", k=6)]
        assert scores == sorted(scores, reverse=True)

    def test_ranking_is_deterministic(self, retriever: BM25Retriever) -> None:
        """Ties break on operation_id so a golden test cannot flake."""
        first = [c.operation_id for c in retriever.top_k("ticket", k=5)]
        second = [c.operation_id for c in retriever.top_k("ticket", k=5)]
        assert first == second

    def test_email_in_the_query_offers_the_resolver_endpoint(
        self, retriever: BM25Retriever
    ) -> None:
        """The blind spot lexical matching cannot cover.

        Nothing in "refund the last invoice for ana@acme.io" matches the words
        "search customers by email", yet no plan can run without that lookup —
        the refund needs a customer id and all the user gave was an address.
        """
        query = "refund the last invoice for ana@acme.io"
        assert retriever.score(query, _index_of(retriever, "search_customers")) == 0.0

        offered = [c.operation_id for c in retriever.top_k(query, k=6)]
        assert "search_customers" in offered
        assert "create_refund" in offered

    def test_resolvers_rank_below_real_lexical_matches(self, retriever: BM25Retriever) -> None:
        """A lookup is a means, never the thing the user asked for."""
        results = retriever.top_k("refund the last invoice for ana@acme.io", k=6)
        by_id = {c.operation_id: c.score for c in results}
        assert by_id["search_customers"] < by_id["create_refund"]

    def test_resolvers_do_not_appear_without_an_identifier(self, retriever: BM25Retriever) -> None:
        offered = [c.operation_id for c in retriever.top_k("refund invoice INV-1007", k=6)]
        assert "search_customers" not in offered

    def test_resolvers_are_read_only(self, retriever: BM25Retriever) -> None:
        """Identifier shape must never pull in something that can change data."""
        resolvers = retriever.resolver_candidates("anything for ana@acme.io")
        assert resolvers
        assert all(not spec.is_write for spec in resolvers)

    def test_resolvers_respect_the_k_budget(self, retriever: BM25Retriever) -> None:
        assert len(retriever.top_k("refund the invoice for ana@acme.io", k=3)) == 3

    def test_everything_offered_still_scores_above_zero(self, retriever: BM25Retriever) -> None:
        results = retriever.top_k("refund the last invoice for ana@acme.io", k=6)
        assert all(c.score > 0 for c in results)

    def test_explain_names_the_query_and_the_winners(self, retriever: BM25Retriever) -> None:
        explanation = retriever.explain("refund invoice", k=2)
        assert "refund invoice" in explanation
        assert "create_refund" in explanation
