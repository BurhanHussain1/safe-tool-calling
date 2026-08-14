"""HTTP routers, one module per resource.

Routers translate HTTP into intent and back. They do not decide whether an
operation is legal — that lives in :mod:`nl2api.mock_api.store`, so a rule can
never be enforced on one route and forgotten on another.
"""

from nl2api.mock_api.routers import customers, invoices, refunds, subscriptions, tickets

#: Registration order controls the order of tags in the OpenAPI document.
ALL_ROUTERS = [
    customers.router,
    subscriptions.router,
    invoices.router,
    tickets.router,
    refunds.router,
]

__all__ = ["ALL_ROUTERS", "customers", "invoices", "refunds", "subscriptions", "tickets"]
