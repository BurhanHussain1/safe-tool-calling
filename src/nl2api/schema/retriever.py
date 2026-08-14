"""Shortlist the endpoints a request could plausibly need.

BM25 over the text of each operation. Written in-house rather than pulled from a
library for two reasons: it is ~50 lines, and the scores need to be inspectable
when a golden test asks *why* an endpoint was or was not offered to the model.

This is a capability boundary as much as a token optimisation. An operation that
never reaches the shortlist cannot appear in a plan, which narrows the blast
radius of a prompt-injection attempt before the validator even runs.

The interface is a Protocol so an embedding-backed retriever can be dropped in
later without touching the planner.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from nl2api.schema.parser import ToolSpec
from nl2api.schema.registry import ToolRegistry

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

#: Words that carry no signal about which endpoint is wanted.
#: Note "get" is here: it is an HTTP verb that appears in half the operation
#: ids, so it separates nothing.
#: noqa SIM905: a whitespace-delimited block reads far better than a 55-element
#: list literal, which ruff would then flatten onto one 390-character line.
_STOPWORDS = frozenset(
    """
    a an and are as at be by can could do does for from get has have how i if in
    into is it its me my of on or please that the their them then there these
    this to us was we were what when where which who will with would you your
    """.split()  # noqa: SIM905
)  # fmt: skip

#: Vocabulary a user brings that the schema does not. Each entry maps a phrase
#: to the schema words it implies. Kept small and explicit — this is a
#: readability aid for the retriever, not a synonym engine.
_QUERY_ALIASES: dict[str, str] = {
    "money back": "refund",
    "refunded": "refund",
    "reimburse": "refund",
    "charge back": "refund",
    "chargeback": "refund",
    "bill": "invoice",
    "billing": "invoice",
    "receipt": "invoice",
    "charged": "invoice",
    "unpaid": "invoice open",
    "outstanding": "invoice open",
    "overdue": "invoice open",
    "downgrade": "change plan subscription",
    "upgrade": "change plan subscription",
    "seats": "subscription plan",
    "renewal": "subscription",
    "churn": "cancel subscription",
    "terminate": "cancel subscription",
    "case": "ticket",
    "issue": "ticket",
    "complaint": "ticket",
    "note": "comment ticket",
    "account": "customer",
    "user": "customer",
    "client": "customer",
    "remove": "delete",
    "erase": "delete",
    "wipe": "delete",
}


#: Identifier shapes a user is likely to paste, mapped to the parameter name
#: that looks them up. Used to offer resolver endpoints that no amount of word
#: matching would surface — see ``BM25Retriever.resolver_candidates``.
_IDENTIFIER_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email"),
)

#: Score given to a resolver added by identifier shape rather than by wording.
#: Positive, so "everything offered scored above zero" still holds, but below
#: any real lexical match so resolvers sort last.
_RESOLVER_SCORE = 1e-6


@dataclass(frozen=True, slots=True)
class ScoredTool:
    """A candidate endpoint and why it was chosen."""

    spec: ToolSpec
    score: float

    @property
    def operation_id(self) -> str:
        return self.spec.operation_id


class Retriever(Protocol):
    """Anything that can shortlist endpoints for a request."""

    def top_k(self, query: str, k: int) -> list[ScoredTool]: ...


def singularize(token: str) -> str:
    """Strip a plural ``s`` so "refunds" and "refund" share an index term.

    Deliberately not a real stemmer. A single conservative rule — drop a
    trailing ``s`` on words longer than three characters that do not end in
    ``ss``, ``us`` or ``is`` — covers this vocabulary (refunds, invoices,
    customers, tickets, subscriptions, seats) without mangling ``status`` or
    ``address``. Anything cleverer would need a test corpus we do not have.
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords, fold plurals."""
    return [
        singularize(token)
        for token in _TOKEN_PATTERN.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


def expand_query(query: str) -> str:
    """Append schema vocabulary implied by the user's wording.

    Appends rather than replaces: the original words still score, so an alias
    can only ever add recall.
    """
    lowered = query.lower()
    additions = [expansion for phrase, expansion in _QUERY_ALIASES.items() if phrase in lowered]
    return " ".join([query, *additions]) if additions else query


class BM25Retriever:
    """Okapi BM25 over the operations in a registry.

    ``k1`` controls how quickly repeated terms stop adding score; ``b`` controls
    how much a long description is penalised relative to a short summary. The
    defaults are the standard ones and have not been tuned — with 17 documents
    there is nothing meaningful to tune against.
    """

    def __init__(self, registry: ToolRegistry, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.registry = registry
        self.k1 = k1
        self.b = b

        self._specs: list[ToolSpec] = registry.all()
        self._documents: list[list[str]] = [tokenize(s.search_text) for s in self._specs]
        self._term_frequencies: list[Counter[str]] = [Counter(d) for d in self._documents]
        self._lengths: list[int] = [len(d) for d in self._documents]
        self._average_length: float = (
            sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        )
        self._inverse_document_frequency: dict[str, float] = self._build_idf()

    def _build_idf(self) -> dict[str, float]:
        total = len(self._documents)
        document_frequency: Counter[str] = Counter()
        for document in self._documents:
            document_frequency.update(set(document))
        # Standard BM25 idf with the +1 smoothing that keeps it non-negative.
        return {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def score(self, query: str, spec_index: int) -> float:
        terms = tokenize(expand_query(query))
        if not terms or self._average_length == 0:
            return 0.0

        frequencies = self._term_frequencies[spec_index]
        length = self._lengths[spec_index]
        normalisation = self.k1 * (1 - self.b + self.b * length / self._average_length)

        total = 0.0
        for term in terms:
            frequency = frequencies.get(term, 0)
            if frequency == 0:
                continue
            idf = self._inverse_document_frequency.get(term, 0.0)
            total += idf * (frequency * (self.k1 + 1)) / (frequency + normalisation)
        return total

    def resolver_candidates(self, query: str) -> list[ToolSpec]:
        """Read-only operations that can resolve an identifier found in the query.

        Lexical matching has a blind spot that matters for multi-step plans.
        "Refund the last invoice for ana@acme.io" scores zero against
        ``search_customers`` — the words "search", "customers" and "email" do
        not appear — yet no plan can run without it, because the refund needs a
        customer id and all the user supplied was an email address.

        The rule is derived from the schema rather than hardcoded: if the query
        contains something shaped like an identifier, offer the read-only
        operations that accept a parameter of that name. Add an endpoint that
        takes an ``email`` parameter and it participates automatically.

        Only read-only operations qualify. A resolver is a lookup, and nothing
        should be pulled into a shortlist by identifier shape alone if it can
        change data.
        """
        # De-duplicated by operation id rather than by object: ToolSpec holds
        # JSON Schema dicts, so it is not hashable.
        wanted: list[ToolSpec] = []
        seen: set[str] = set()
        for pattern, parameter_name in _IDENTIFIER_HINTS:
            if not pattern.search(query):
                continue
            for spec in self._specs:
                if spec.is_write or spec.operation_id in seen:
                    continue
                if spec.parameter(parameter_name) is not None:
                    seen.add(spec.operation_id)
                    wanted.append(spec)
        return wanted

    def top_k(self, query: str, k: int = 6) -> list[ScoredTool]:
        """The ``k`` best-matching operations, highest score first.

        Zero-scoring operations are excluded: offering the model an endpoint
        with no connection to the request is an invitation to misuse it. A query
        that matches nothing returns an empty list, and the planner turns that
        into a clarifying question rather than a guess.

        Resolver operations (see :meth:`resolver_candidates`) are appended when
        the query carries an identifier they can look up, trimming the lexical
        results to stay within ``k``. They rank last because a lookup is a means,
        never the thing the user asked for.
        """
        scored = [
            ScoredTool(spec=spec, score=self.score(query, index))
            for index, spec in enumerate(self._specs)
        ]
        ranked = sorted(
            (s for s in scored if s.score > 0),
            key=lambda s: (-s.score, s.operation_id),
        )

        already = {s.operation_id for s in ranked[:k]}
        resolvers = [
            ScoredTool(spec=spec, score=_RESOLVER_SCORE)
            for spec in self.resolver_candidates(query)
            if spec.operation_id not in already
        ]
        if not resolvers:
            return ranked[:k]

        room = max(k - len(resolvers), 1)
        return [*ranked[:room], *resolvers][:k]

    def explain(self, query: str, k: int = 6) -> str:
        """Human-readable ranking, for debugging a surprising shortlist."""
        lines = [f"query: {query!r}", f"expanded: {expand_query(query)!r}", ""]
        lines.extend(
            f"  {s.score:6.3f}  {s.operation_id:32} {s.spec.signature}"
            for s in self.top_k(query, k)
        )
        return "\n".join(lines)
