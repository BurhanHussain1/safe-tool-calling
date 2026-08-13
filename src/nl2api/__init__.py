"""nl2api — a natural language interface that turns requests into safe API calls.

The package is layered so that each layer can be tested in isolation:

    mock_api     the business system we are driving (FastAPI + OpenAPI)
    schema       OpenAPI document -> typed tool catalogue + retrieval
    planner      user request -> strictly typed call plan (LLM proposes)
    guardrails   plan -> validated, policy-checked, dry-run plan (code decides)
    executor     validated plan -> HTTP calls with typed workflow state
    persistence  audit trail for every plan, call and approval
    service      the assistant's own HTTP API
    ui           Streamlit workflow front-end

The invariant that holds the design together: the LLM only ever *proposes*.
Deterministic code decides what actually runs.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
