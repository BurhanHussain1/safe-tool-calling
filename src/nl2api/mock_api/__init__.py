"""A mock SaaS admin system — the business API the assistant drives.

This package exists to be a *realistic contract*, not a realistic product. What
matters is that its OpenAPI document carries everything a planner needs to reason
safely: parameters, response shapes, and — via the ``x-risk`` / ``x-required-roles``
extensions — how dangerous each operation is and who may call it.

Risk lives on the endpoint rather than in a separate registry so that adding an
operation forces you to declare its blast radius in the same edit.
"""

from nl2api.mock_api.main import app, create_app

__all__ = ["app", "create_app"]
