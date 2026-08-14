"""Turn an OpenAPI document into a typed tool catalogue the planner can use.

    parser.py     OpenAPI operation -> ToolSpec (refs inlined, risk read)
    registry.py   the collection: lookup by operation_id, render for a prompt
    retriever.py  shortlist the handful of endpoints a request could need

The shortlist is a capability boundary, not just a token optimisation: a plan
cannot reference ``delete_customer`` if ``delete_customer`` was never put in
front of the model for that request.
"""

from nl2api.schema.parser import ParameterSpec, ToolSpec, parse_openapi
from nl2api.schema.registry import ToolRegistry, UnknownOperation
from nl2api.schema.retriever import BM25Retriever, ScoredTool

__all__ = [
    "BM25Retriever",
    "ParameterSpec",
    "ScoredTool",
    "ToolRegistry",
    "ToolSpec",
    "UnknownOperation",
    "parse_openapi",
]
