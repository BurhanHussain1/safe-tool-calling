"""Mock business API — app factory and OpenAPI enrichment.

The interesting part of this module is :func:`build_openapi`. FastAPI generates a
schema describing *what* each operation does; we walk the route table and add
*how dangerous it is* and *who may call it*. The result is a single document that
carries the whole contract, which is what makes the planner schema-aware rather
than prompt-aware.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from nl2api.config import get_settings
from nl2api.mock_api.deps import get_role
from nl2api.mock_api.models import ErrorResponse
from nl2api.mock_api.risk import RiskMetadata, get_risk_metadata
from nl2api.mock_api.routers import ALL_ROUTERS
from nl2api.mock_api.store import StoreError

logger = logging.getLogger(__name__)

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

API_TITLE = "Mock SaaS Admin API"
API_VERSION = "1.0.0"
API_DESCRIPTION = """
A mock SaaS admin system: customers, subscriptions, invoices, support tickets
and refunds.

Every operation declares its own blast radius as OpenAPI vendor extensions:

| Extension | Meaning |
| --- | --- |
| `x-risk` | `read_only`, `low_risk_write` or `high_risk_write` |
| `x-required-roles` | Every role permitted to call the operation |
| `x-side-effects` | One plain-English sentence, written for a human approver |
| `x-idempotent` | Whether repeating the call is safe |

Callers identify themselves with an `X-Role` header (`viewer`,
`support_agent` or `billing_admin`) and optionally an `X-Actor` header
recording who performed a write.
""".strip()

#: Errors every operation can return, merged into each response spec so a
#: consumer never has to guess the shape of a failure.
_COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Malformed request or unknown role."},
    403: {"model": ErrorResponse, "description": "Caller's role is insufficient."},
    404: {"model": ErrorResponse, "description": "Resource does not exist."},
    409: {"model": ErrorResponse, "description": "Conflicts with the resource's current state."},
    422: {"model": ErrorResponse, "description": "Violates a business rule."},
}


def build_risk_index(routers: Iterable[APIRouter]) -> dict[str, RiskMetadata]:
    """Map ``operation_id`` to the risk metadata declared on its endpoint.

    Keyed by operation id rather than by path+method because that is the
    identity the planner addresses endpoints by, and because it lets us read the
    metadata off the ``APIRouter`` objects we own instead of walking
    ``app.routes``. FastAPI 0.141 made router inclusion lazy — included routes
    are no longer reachable from ``app.routes`` — so anything that walks the
    application's route table is coupled to framework internals. This is not.
    """
    index: dict[str, RiskMetadata] = {}
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            metadata = get_risk_metadata(route.endpoint)
            if metadata is None:
                continue
            if not route.operation_id:
                logger.warning(
                    "Route %s has @risk metadata but no explicit operation_id; skipping.",
                    route.path,
                )
                continue
            index[route.operation_id] = metadata
    return index


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Generate the OpenAPI document and inject risk metadata into it.

    Cached on the app after the first call, which is FastAPI's usual contract for
    a custom ``openapi()``.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        routes=app.routes,
    )

    risk_index = build_risk_index(ALL_ROUTERS)
    undeclared: list[str] = []

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            metadata = risk_index.get(operation.get("operationId", ""))
            if metadata is None:
                undeclared.append(f"{method.upper()} {path}")
                continue
            operation.update(metadata.as_openapi_extensions())

    if undeclared:
        # Loud, because an endpoint without declared risk is exactly the hole
        # this design exists to close. Tests assert this list is empty.
        logger.warning("Operations missing @risk metadata: %s", ", ".join(sorted(undeclared)))

    schema["x-role-hierarchy"] = {
        "description": "Roles are cumulative: billing_admin implies support_agent implies viewer.",
        "order": ["viewer", "support_agent", "billing_admin"],
    }
    app.openapi_schema = schema
    return schema


def create_app() -> FastAPI:
    """Build the mock API application."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        responses=_COMMON_ERROR_RESPONSES,
        # Resolve the caller's role on every request, including read-only ones.
        # Without this an unrecognised X-Role would be silently ignored on any
        # route that has no explicit role requirement — a header that looks like
        # it is doing something while doing nothing is worse than no header.
        dependencies=[Depends(get_role)],
    )

    for router in ALL_ROUTERS:
        app.include_router(router)

    @app.get("/health", tags=["meta"], summary="Liveness probe", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "mock-api", "version": API_VERSION}

    @app.exception_handler(StoreError)
    async def store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
        """Translate a business-rule rejection into the uniform error envelope."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Report schema violations in the same envelope as business rules.

        One error shape for every failure means the executor can classify
        problems without sniffing which layer produced them.
        """
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_failed",
                    "message": f"Request does not match the schema — {problems}",
                }
            },
        )

    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
    return app


app = create_app()
