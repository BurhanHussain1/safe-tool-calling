"""HTTP transport to the business API.

Thin on purpose. The only judgement it makes is about retries: a connection
that never opened may be retried once, and a response that arrived is never
retried. Retrying a 500 on ``create_refund`` — an operation the schema declares
non-idempotent — is how one refund becomes two.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nl2api.config import Role, Settings, get_settings
from nl2api.guardrails.validator import ResolvedCall
from nl2api.mock_api.deps import ACTOR_HEADER, ROLE_HEADER

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """What came back, normalised."""

    status_code: int
    body: Any
    latency_ms: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def error_message(self) -> str | None:
        """The message from the API's uniform error envelope, if there is one."""
        if self.ok:
            return None
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"{error.get('code', 'error')}: {error['message']}"
            detail = self.body.get("detail")
            if isinstance(detail, dict) and detail.get("message"):
                return f"{detail.get('code', 'error')}: {detail['message']}"
        return f"HTTP {self.status_code}"


class ApiClient:
    """Calls the business API on behalf of a role."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        actor: str = "assistant",
    ) -> None:
        self.settings = settings or get_settings()
        self.actor = actor
        self._client = client or httpx.Client(
            base_url=self.settings.mock_api_url,
            timeout=self.settings.request_timeout_seconds,
        )

    def send(self, call: ResolvedCall, role: Role) -> ApiResponse:
        """Issue one validated call. Never raises for an HTTP error status."""
        if call.has_deferred_values:
            raise ValueError(
                f"Step {call.step_id} still has unresolved values; resolve before sending."
            )

        headers = {ROLE_HEADER: role.value, ACTOR_HEADER: self.actor}
        started = time.perf_counter()

        try:
            response = self._request(call, headers)
        except httpx.ConnectError as exc:
            # The request never reached the server, so nothing can have changed.
            # This is the only case where retrying is provably safe.
            logger.info("Connection failed for %s; retrying once.", call.step_id)
            try:
                response = self._request(call, headers)
            except httpx.HTTPError as retry_exc:
                raise TransportError(str(retry_exc)) from exc
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        elapsed = int((time.perf_counter() - started) * 1000)
        return ApiResponse(
            status_code=response.status_code,
            body=_decode(response),
            latency_ms=elapsed,
        )

    def _request(self, call: ResolvedCall, headers: dict[str, str]) -> httpx.Response:
        return self._client.request(
            method=call.method,
            url=call.url_path(),
            params=call.query_params or None,
            json=call.body if call.body else None,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class TransportError(RuntimeError):
    """The call could not be delivered at all."""


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text
