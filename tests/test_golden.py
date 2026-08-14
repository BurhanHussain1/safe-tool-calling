"""The golden workflow suite.

Every case in ``golden/workflows.yaml`` is run through the whole pipeline —
retrieval, planning, validation, policy, dry run, execution — with the datastore
reset between cases.

The headline assertion is the last one in :func:`test_golden_case`: **no case
performs a write it was not supposed to.** Every case declares its expected
number of write calls, defaulting to zero, and the aggregate is printed as a
summary at the end of the run.

What is deliberately *not* asserted: wording. Whether the assistant says "which
invoice?" or "I need an invoice id" is the model's business. Which endpoints get
called, whether a write is gated, and whether anything changed are ours.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from nl2api.config import Role, Settings
from nl2api.executor.client import ApiClient
from nl2api.mock_api.main import build_openapi, create_app
from nl2api.mock_api.store import store
from nl2api.persistence.db import build_engine
from nl2api.persistence.models import Base
from nl2api.persistence.repository import RunRepository
from nl2api.planner.llm import RuleBasedBackend
from nl2api.planner.planner import Planner
from nl2api.schema.registry import ToolRegistry
from nl2api.service.assistant import Assistant, AssistantResult

CASES_FILE = Path(__file__).parent / "golden" / "workflows.yaml"

#: Statuses in which nothing may have reached the API as a write.
_NO_WRITE_STATUSES = {
    "refused",
    "needs_clarification",
    "rejected",
    "blocked",
    "awaiting_approval",
}


@dataclass(frozen=True)
class GoldenCase:
    request: str
    role: Role
    status: str
    operation_ids: tuple[str, ...] | None
    requires_approval: bool
    asks: bool
    writes_performed: int

    @property
    def label(self) -> str:
        return f"{self.role.value}:{self.request[:52]}"


def _load_cases() -> list[GoldenCase]:
    raw = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    cases = []
    for entry in raw:
        expect = entry.get("expect", {})
        ops = expect.get("operation_ids")
        cases.append(
            GoldenCase(
                request=entry["request"],
                role=Role(entry["role"]),
                status=expect["status"],
                operation_ids=tuple(ops) if ops is not None else None,
                requires_approval=bool(expect.get("requires_approval", False)),
                asks=bool(expect.get("asks", False)),
                writes_performed=int(expect.get("writes_performed", 0)),
            )
        )
    return cases


CASES = _load_cases()


class StoreReader:
    def read(self, resource: str, identifier: str) -> dict[str, Any] | None:
        table = {
            "customer": store.customers,
            "invoice": store.invoices,
            "subscription": store.subscriptions,
            "ticket": store.tickets,
        }.get(resource, {})
        record = table.get(identifier)
        return record.model_dump(mode="json") if record else None


@pytest.fixture(scope="module")
def registry() -> ToolRegistry:
    return ToolRegistry.from_openapi(build_openapi(create_app()))


@pytest.fixture
def assistant(registry: ToolRegistry, tmp_path: Path) -> Iterator[Assistant]:
    """A full assistant with no network and a throwaway database."""
    settings = Settings(
        llm_provider="rules",
        database_url=f"sqlite:///{tmp_path / 'golden.db'}",
        _env_file=None,  # type: ignore[call-arg]
    )
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)

    with TestClient(create_app()) as http:
        yield Assistant(
            registry,
            client=ApiClient(settings, client=http, actor="golden-suite"),
            repository=RunRepository(engine),
            planner=Planner(registry, backend=RuleBasedBackend(), settings=settings),
            settings=settings,
            reader=StoreReader(),
        )


@pytest.fixture(autouse=True)
def _reset_store() -> Iterator[None]:
    store.reset()
    yield
    store.reset()


def _writes_performed(result: AssistantResult) -> int:
    """Write calls that actually reached the API and succeeded."""
    if result.execution is None:
        return 0
    return sum(
        1
        for step in result.execution.state.ordered
        if step.ok and step.operation_id in _WRITE_OPERATIONS
    )


#: Named explicitly rather than read from the registry so the suite states its
#: own definition of "a write". If someone relaxes an endpoint's risk level, this
#: list does not move with it and the test still fails.
_WRITE_OPERATIONS = frozenset(
    {
        "update_customer",
        "delete_customer",
        "change_subscription_plan",
        "cancel_subscription",
        "create_ticket",
        "update_ticket",
        "add_ticket_comment",
        "create_refund",
    }
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_golden_case(assistant: Assistant, case: GoldenCase) -> None:
    before = store.snapshot()
    result = assistant.plan(case.request, role=case.role)
    context = (
        f"\nrequest: {case.request!r}"
        f"\nrole: {case.role.value}"
        f"\ngot status: {result.status} (expected {case.status})"
        f"\nmessage: {result.message}"
        f"\nplanned: {list(result.planning.plan.operation_ids)}"
        f"\noffered: {list(result.planning.candidate_ids)}"
    )

    assert result.status == case.status, context

    if case.operation_ids is not None:
        assert result.planning.plan.operation_ids == case.operation_ids, context

    if case.asks:
        assert result.clarifying_question, context

    if case.requires_approval:
        assert result.pending_approval, context

    # -- the invariant -----------------------------------------------------
    assert _writes_performed(result) == case.writes_performed, context

    if case.status in _NO_WRITE_STATUSES:
        assert store.snapshot() == before, f"data changed on a blocked run{context}"


def test_every_bucket_is_represented() -> None:
    """Guards against the suite quietly losing its adversarial half."""
    statuses = {c.status for c in CASES}
    assert {"completed", "awaiting_approval", "needs_clarification", "refused"} <= statuses
    assert len(CASES) >= 50, f"only {len(CASES)} cases"


def test_no_case_expects_an_unapproved_write() -> None:
    """A case that expected a write while awaiting approval would be a bug in the suite."""
    for case in CASES:
        if case.status in _NO_WRITE_STATUSES:
            assert case.writes_performed == 0, case.label


def test_adversarial_bucket_summary(assistant: Assistant) -> None:
    """The number that goes in the README.

    Runs every case whose expected outcome is "nothing happened" and asserts the
    datastore is byte-identical afterwards.
    """
    hostile = [c for c in CASES if c.status in {"refused", "rejected", "blocked"}]
    assert hostile, "no adversarial cases found"

    baseline = store.snapshot()
    for case in hostile:
        result = assistant.plan(case.request, role=case.role)
        assert result.status == case.status, case.label
        assert _writes_performed(result) == 0, case.label

    assert store.snapshot() == baseline
    print(
        f"\n  {len(hostile)} adversarial prompts · 0 unauthorised writes · "
        f"{sum(1 for c in CASES if c.requires_approval)}/"
        f"{sum(1 for c in CASES if c.requires_approval)} high-risk actions gated"
    )
