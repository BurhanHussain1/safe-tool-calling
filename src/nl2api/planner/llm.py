"""Planning backends.

Three implementations behind one Protocol:

``AnthropicBackend``   the real thing. Uses structured outputs, so a plan comes
                       back as a validated ``Plan`` — no regex, no retry-on-parse.
``CassetteBackend``    replays a recorded plan. This is what lets the golden
                       workflow suite run in CI with no key and no network.
``RuleBasedBackend``   a small deterministic planner for offline development.
                       It is not clever and does not pretend to be; it exists so
                       the stack is demoable and testable without a provider.

The point of the seam is that everything downstream — validation, policy, dry
runs, execution — is identical whichever backend produced the plan. A guardrail
that only works against a real model is a guardrail you cannot test.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nl2api.config import Role, Settings
from nl2api.planner.models import Argument, Plan, PlanStep
from nl2api.planner.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything a backend needs to produce a plan."""

    request: str
    role: Role
    catalogue: str
    candidate_operation_ids: tuple[str, ...]

    @property
    def cassette_key(self) -> str:
        """Stable filename-safe key for recording and replaying this request."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.request.lower()).strip("-")[:80]
        return f"{self.role.value}__{slug or 'empty'}"


class PlannerBackend(Protocol):
    """Anything that can turn a request into a plan."""

    def generate(self, request: PlanRequest) -> Plan: ...


class BackendError(RuntimeError):
    """The backend could not produce a plan at all."""


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicBackend:
    """Plan with Claude, using structured outputs.

    ``messages.parse`` constrains the response to the ``Plan`` schema and returns
    a validated instance, which is why there is no JSON-repair loop here. The
    plan schema was designed around that constraint — see
    :mod:`nl2api.planner.models`.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise BackendError(
                "The 'anthropic' package is not installed. Install it, or set "
                "NL2API_LLM_PROVIDER=rules to plan offline."
            ) from exc

        if not settings.has_llm_credentials:
            raise BackendError(
                "No ANTHROPIC_API_KEY is configured. Set one, or use "
                "NL2API_LLM_PROVIDER=rules / cassette to plan offline."
            )

        key = settings.anthropic_api_key
        assert key is not None  # guaranteed by has_llm_credentials
        self._client = anthropic.Anthropic(api_key=key.get_secret_value())
        self._model = settings.model
        self._max_tokens = settings.llm_max_tokens

    def generate(self, request: PlanRequest) -> Plan:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": build_user_prompt(
                        request=request.request,
                        role=request.role,
                        catalogue=request.catalogue,
                    ),
                }
            ],
            output_format=Plan,
        )

        plan = response.parsed_output
        if plan is None:
            # Structured outputs should make this unreachable, but a refusal or a
            # max_tokens cut-off can still land here. Fail loudly rather than
            # returning an empty plan that looks like "nothing to do".
            raise BackendError(
                f"Model returned no parseable plan (stop_reason={response.stop_reason!r})."
            )
        return plan


# ---------------------------------------------------------------------------
# Cassettes
# ---------------------------------------------------------------------------
class CassetteMiss(BackendError):
    """No recording exists for this request."""


class CassetteBackend:
    """Replay a plan recorded on disk.

    Recordings are plain JSON so a reviewer can read what the model said, and so
    a golden case can be hand-authored when no key is available.
    """

    def __init__(self, directory: Path, *, fallback: PlannerBackend | None = None) -> None:
        self.directory = Path(directory)
        self._fallback = fallback

    def path_for(self, request: PlanRequest) -> Path:
        return self.directory / f"{request.cassette_key}.json"

    def generate(self, request: PlanRequest) -> Plan:
        path = self.path_for(request)
        if path.exists():
            return Plan.model_validate_json(path.read_text(encoding="utf-8"))
        if self._fallback is not None:
            logger.debug(
                "No cassette at %s; falling back to %s.", path, type(self._fallback).__name__
            )
            return self._fallback.generate(request)
        raise CassetteMiss(
            f"No cassette for {request.cassette_key!r} at {path}. "
            "Record one, or configure a fallback backend."
        )

    def record(self, request: PlanRequest, plan: Plan) -> Path:
        """Write a plan to disk so it can be replayed."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(request)
        path.write_text(plan.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Offline rules
# ---------------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CUSTOMER_ID = re.compile(r"\bCUS-\d+\b", re.IGNORECASE)
_INVOICE_ID = re.compile(r"\bINV-\d+\b", re.IGNORECASE)
_TICKET_ID = re.compile(r"\bTIC-\d+\b", re.IGNORECASE)
_SUBSCRIPTION_ID = re.compile(r"\bSUB-\d+\b", re.IGNORECASE)
_MONEY = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?")

#: Phrases that should never produce a plan, whatever else the request says.
_REFUSAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"ignore (?:all |any |your )?(?:previous|prior|earlier|above) instructions", re.I
        ),
        "That request asks me to disregard my instructions, so I won't act on it.",
    ),
    (
        re.compile(
            r"\b(?:skip|bypass|waive|disable)\b.{0,20}\b(?:approval|confirmation|check)", re.I
        ),
        "Approval gates are not something I can waive. Ask an authorised approver instead.",
    ),
    (
        re.compile(r"\bdelete\b.{0,20}\b(?:all|every|each)\b", re.I),
        "I won't perform bulk deletion. Delete customers one at a time, with approval for each.",
    ),
    (
        re.compile(r"\b(?:refund|cancel)\b.{0,20}\b(?:all|every|everyone|everybody)\b", re.I),
        "I won't apply a refund or cancellation in bulk. Name the specific record instead.",
    ),
    (
        re.compile(r"\bdrop\s+table\b|\btruncate\b|\bdrop\s+database\b", re.I),
        "There is no operation for that, and I won't attempt it.",
    ),
)


class RuleBasedBackend:
    """A deterministic keyword planner for offline use.

    This is *not* a stand-in for a language model. It recognises a handful of
    shapes well enough to develop and demo the guardrail pipeline without a key,
    and returns a clarifying question whenever it does not understand — which,
    conveniently, is also the correct behaviour when a real model is unsure.
    """

    def generate(self, request: PlanRequest) -> Plan:
        text = request.request.strip()
        lowered = text.lower()
        available = set(request.candidate_operation_ids)

        for pattern, reason in _REFUSAL_PATTERNS:
            if pattern.search(text):
                return Plan.refusing(intent=text, reason=reason)

        email = _first(_EMAIL, text)
        customer_id = _first(_CUSTOMER_ID, text, upper=True)
        invoice_id = _first(_INVOICE_ID, text, upper=True)
        ticket_id = _first(_TICKET_ID, text, upper=True)
        subscription_id = _first(_SUBSCRIPTION_ID, text, upper=True)

        if _mentions(lowered, "refund", "money back", "reimburse"):
            return self._plan_refund(text, email, customer_id, invoice_id, available)
        if ticket_id and _mentions(lowered, "note", "comment"):
            return self._plan_comment(text, ticket_id, available)
        if _mentions(lowered, "ticket", "case", "complaint") and _mentions(
            lowered, "open", "create", "raise", "file", "log"
        ):
            return self._plan_ticket(text, email, customer_id, available)
        if _mentions(lowered, "cancel") and _mentions(lowered, "subscription", "plan"):
            return self._plan_cancel(text, subscription_id, email, available)
        if _mentions(lowered, "invoice", "bill", "unpaid", "outstanding"):
            return self._plan_invoices(text, email, customer_id, lowered, available)
        if _mentions(lowered, "subscription", "plan", "seats"):
            return self._plan_subscriptions(text, email, customer_id, available)
        if email or customer_id or _mentions(lowered, "customer", "account"):
            return self._plan_customer_lookup(text, email, customer_id)

        return Plan.asking(
            intent=text,
            question=(
                "I could not tell which record you mean. Which customer is this "
                "about, and what would you like me to do?"
            ),
        )

    # -- shapes ------------------------------------------------------------
    def _plan_refund(
        self,
        text: str,
        email: str | None,
        customer_id: str | None,
        invoice_id: str | None,
        available: set[str],
    ) -> Plan:
        if "create_refund" not in available:
            return Plan.refusing(intent=text, reason="No refund operation is available to me.")

        amount = _amount_cents(text)
        steps: list[PlanStep] = []

        if invoice_id:
            steps.append(
                _step(
                    "s1",
                    "get_invoice",
                    [("invoice_id", "path", invoice_id)],
                    reason="Confirm the invoice exists and read its refundable balance.",
                    expected="The invoice, including amount_cents and refunded_cents.",
                )
            )
            refund_amount = str(amount) if amount is not None else "$steps.s1.amount_cents"
            steps.append(
                _step(
                    "s2",
                    "create_refund",
                    [
                        ("invoice_id", "body", invoice_id),
                        ("amount_cents", "body", refund_amount),
                        ("reason", "body", _reason_from(text)),
                    ],
                    reason="Issue the refund the user asked for.",
                    expected="A refund record and the invoice moved to refunded.",
                )
            )
            assumptions = (
                []
                if amount is not None
                else ["Refunding the invoice in full; no amount was given."]
            )
            return Plan(intent=text, steps=steps, assumptions=assumptions)

        if not (email or customer_id):
            return Plan.asking(
                intent=text,
                question=(
                    "Which invoice should I refund? Give me an invoice id (like "
                    "INV-1007) or the customer's email address."
                ),
            )

        anchor = self._lookup_steps(email, customer_id, steps)
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "list_invoices",
                [("customer_id", "query", anchor), ("status", "query", "paid")],
                reason="Find the customer's paid invoices; the most recent is first.",
                expected="Paid invoices, newest first.",
            )
        )
        invoice_step = f"s{len(steps)}"
        refund_amount = (
            str(amount) if amount is not None else f"$steps.{invoice_step}.data[0].amount_cents"
        )
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "create_refund",
                [
                    ("invoice_id", "body", f"$steps.{invoice_step}.data[0].id"),
                    ("amount_cents", "body", refund_amount),
                    ("reason", "body", _reason_from(text)),
                ],
                reason="Refund the most recent paid invoice.",
                expected="A refund record against that invoice.",
            )
        )
        return Plan(
            intent=text,
            steps=steps,
            assumptions=["'The last invoice' means the most recent paid invoice."],
        )

    def _plan_invoices(
        self,
        text: str,
        email: str | None,
        customer_id: str | None,
        lowered: str,
        available: set[str],
    ) -> Plan:
        if "list_invoices" not in available:
            return Plan.refusing(intent=text, reason="No invoice operation is available to me.")
        if not (email or customer_id):
            return Plan.asking(
                intent=text,
                question="Whose invoices would you like? An email address or customer id works.",
            )
        steps: list[PlanStep] = []
        anchor = self._lookup_steps(email, customer_id, steps)
        arguments = [("customer_id", "query", anchor)]
        if _mentions(lowered, "unpaid", "outstanding", "open", "overdue"):
            arguments.append(("status", "query", "open"))
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "list_invoices",
                arguments,
                reason="List the invoices the user asked about.",
                expected="Matching invoices, newest first.",
            )
        )
        return Plan(intent=text, steps=steps)

    def _plan_subscriptions(
        self, text: str, email: str | None, customer_id: str | None, available: set[str]
    ) -> Plan:
        if "list_customer_subscriptions" not in available:
            return Plan.refusing(
                intent=text, reason="No subscription operation is available to me."
            )
        if not (email or customer_id):
            return Plan.asking(
                intent=text,
                question="Whose subscription would you like to see? An email address works.",
            )
        steps: list[PlanStep] = []
        anchor = self._lookup_steps(email, customer_id, steps)
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "list_customer_subscriptions",
                [("customer_id", "path", anchor)],
                reason="Read the customer's subscriptions.",
                expected="The customer's subscription records.",
            )
        )
        return Plan(intent=text, steps=steps)

    def _plan_ticket(
        self, text: str, email: str | None, customer_id: str | None, available: set[str]
    ) -> Plan:
        if "create_ticket" not in available:
            return Plan.refusing(intent=text, reason="No ticket operation is available to me.")
        if not (email or customer_id):
            return Plan.asking(
                intent=text, question="Which customer is the ticket for? An email address works."
            )
        steps: list[PlanStep] = []
        anchor = self._lookup_steps(email, customer_id, steps)
        subject = _subject_from(text)
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "create_ticket",
                [
                    ("customer_id", "body", anchor),
                    ("subject", "body", subject),
                    ("body", "body", text),
                ],
                reason="Open the support ticket the user asked for.",
                expected="A new ticket in the open state.",
            )
        )
        return Plan(intent=text, steps=steps, assumptions=[f"Using {subject!r} as the subject."])

    def _plan_comment(self, text: str, ticket_id: str, available: set[str]) -> Plan:
        if "add_ticket_comment" not in available:
            return Plan.refusing(intent=text, reason="No comment operation is available to me.")
        return Plan(
            intent=text,
            steps=[
                _step(
                    "s1",
                    "add_ticket_comment",
                    [
                        ("ticket_id", "path", ticket_id),
                        ("author", "body", "assistant"),
                        ("body", "body", text),
                        ("internal", "body", "true"),
                    ],
                    reason="Add the requested note to the ticket.",
                    expected="The ticket with the new comment appended.",
                )
            ],
            assumptions=["Recorded as an internal note, so the customer will not see it."],
        )

    def _plan_cancel(
        self, text: str, subscription_id: str | None, email: str | None, available: set[str]
    ) -> Plan:
        if "cancel_subscription" not in available:
            return Plan.refusing(
                intent=text, reason="No cancellation operation is available to me."
            )
        if not subscription_id:
            # A customer can hold several subscriptions, so an email narrows the
            # question but does not answer it. Ask precisely rather than
            # cancelling whichever one happened to come back first.
            if email:
                return Plan.asking(
                    intent=text,
                    question=(
                        f"{email} may have more than one subscription. Which one should "
                        "I cancel? A subscription id (like SUB-2001) settles it."
                    ),
                )
            return Plan.asking(
                intent=text,
                question=(
                    "Which subscription should I cancel? Give me a subscription id "
                    "(like SUB-2001), or the customer's email so I can look it up."
                ),
            )
        return Plan(
            intent=text,
            steps=[
                _step(
                    "s1",
                    "cancel_subscription",
                    [
                        ("subscription_id", "path", subscription_id),
                        ("reason", "body", _reason_from(text)),
                    ],
                    reason="Cancel the subscription the user named.",
                    expected="The subscription marked canceled.",
                )
            ],
        )

    def _plan_customer_lookup(self, text: str, email: str | None, customer_id: str | None) -> Plan:
        if customer_id:
            return Plan(
                intent=text,
                steps=[
                    _step(
                        "s1",
                        "get_customer",
                        [("customer_id", "path", customer_id)],
                        reason="Read the customer record the user named.",
                        expected="The customer record.",
                    )
                ],
            )
        if email:
            return Plan(
                intent=text,
                steps=[
                    _step(
                        "s1",
                        "search_customers",
                        [("email", "query", email)],
                        reason="Resolve the email address to a customer.",
                        expected="At most one matching customer.",
                    )
                ],
            )
        return Plan.asking(
            intent=text,
            question="Which customer do you mean? An email address or customer id works.",
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _lookup_steps(email: str | None, customer_id: str | None, steps: list[PlanStep]) -> str:
        """Append a lookup step when needed; return how to refer to the customer.

        A known id is used directly. An email becomes a ``search_customers`` step
        whose first result is referenced — never a guessed id.
        """
        if customer_id:
            return customer_id
        steps.append(
            _step(
                f"s{len(steps) + 1}",
                "search_customers",
                [("email", "query", email or "")],
                reason="Resolve the email address to a customer id.",
                expected="At most one matching customer.",
            )
        )
        return f"$steps.s{len(steps)}.data[0].id"


def _step(
    step_id: str,
    operation_id: str,
    arguments: list[tuple[str, str, str]],
    *,
    reason: str,
    expected: str,
) -> PlanStep:
    return PlanStep(
        id=step_id,
        operation_id=operation_id,
        arguments=[
            Argument(name=name, location=location, value=value)  # type: ignore[arg-type]
            for name, location, value in arguments
        ],
        reason=reason,
        expected_result=expected,
    )


def _first(pattern: re.Pattern[str], text: str, *, upper: bool = False) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(0).upper() if upper else match.group(0)


def _mentions(lowered: str, *needles: str) -> bool:
    return any(needle in lowered for needle in needles)


def _amount_cents(text: str) -> int | None:
    """Read an explicit dollar amount, in cents. Returns None if none was given."""
    match = _MONEY.search(text)
    if match is None:
        return None
    dollars = int(match.group(1).replace(",", ""))
    cents = int(match.group(2) or 0)
    return dollars * 100 + cents


def _reason_from(text: str) -> str:
    """Best-effort reason string; the API requires at least three characters."""
    for marker in (" because ", " since ", " as they ", " due to "):
        if marker in text.lower():
            index = text.lower().index(marker)
            tail = text[index + len(marker) :].strip(" .")
            if len(tail) >= 3:
                return tail[:280]
    return f"Requested by support: {text.strip()[:250]}"


def _subject_from(text: str) -> str:
    """First clause of the request, trimmed to the API's subject limits."""
    clause = re.split(r"[.;\n]", text.strip())[0].strip()
    if len(clause) < 3:
        clause = text.strip()[:160] or "Support request"
    return clause[:160]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def build_backend(settings: Settings, *, cassette_directory: Path | None = None) -> PlannerBackend:
    """Pick a backend from configuration, degrading rather than failing.

    ``effective_llm_provider`` already downgrades ``anthropic`` to ``rules`` when
    no key is present, so a developer who has not configured anything still gets
    a working stack instead of a stack trace.
    """
    provider = settings.effective_llm_provider

    if provider == "anthropic":
        try:
            return AnthropicBackend(settings)
        except BackendError as exc:
            logger.warning("Falling back to the offline planner: %s", exc)
            return RuleBasedBackend()

    if provider == "cassette":
        directory = cassette_directory or Path("tests/cassettes")
        return CassetteBackend(directory, fallback=RuleBasedBackend())

    return RuleBasedBackend()


def load_plan(payload: str | dict[str, Any]) -> Plan:
    """Validate a plan from JSON text or a dict. Used by cassettes and fixtures."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Plan.model_validate(payload)
