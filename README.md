# safe-tool-calling

[![CI](https://github.com/YOUR-USERNAME/safe-tool-calling/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR-USERNAME/safe-tool-calling/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Safe tool use over real API contracts.**

A natural-language interface that turns requests into safe API calls. An LLM
proposes a plan; deterministic code validates it against an OpenAPI schema,
previews what each write would do in plain English, and blocks anything risky
until a human approves it.

```
"Refund the last invoice for ana@acme.io"           role: billing_admin

  ├─ offered      6 of 17 endpoints  (delete_customer was never on the list)
  ├─ planned      s1 search_customers → s2 list_invoices → s3 create_refund
  ├─ validated    ✓ schema   ✓ role   ✓ $steps references resolve
  ├─ dry run      Refund $240.00 against invoice INV-1007 (Ana Ruiz).
  │                 status: paid → refunded
  │                 refunded_cents: 0 → 24000
  │                 ⛔ This cannot be undone.
  │                 ⚠ Repeating this call would issue a second refund.
  └─ BLOCKED      awaiting approval   ▸ [ Approve ]  [ Reject ]
```

---

## Results

```
54 golden workflows · 10 adversarial prompts · 0 unauthorised writes
 8/8 high-risk actions gated ·  6/6 ambiguous requests asked instead of guessing
355 tests, no network, no API key required
```

| Bucket | Cases | Asserted outcome |
| --- | --- | --- |
| Read-only | 12 | executed immediately |
| Multi-step chains | 12 | identifiers passed through typed state |
| Low-risk writes | 6 | executed and reported |
| High-risk writes | 8 | **blocked** pending human approval |
| Ambiguous | 6 | **asked** a clarifying question, zero writes |
| Injection / bulk destruction | 6 | **refused**, zero writes |
| Under-privileged caller | 4 | **refused**, zero writes |

For every case whose expected outcome is "nothing happened", the datastore is
snapshotted before and after and asserted byte-identical.

---

## Quickstart

```bash
docker compose up --build
```

| Service | URL |
| --- | --- |
| Workflow UI | http://localhost:8501 |
| Assistant API | http://localhost:8001/docs |
| Business API | http://localhost:8000/docs |

**No API key is required.** Without `ANTHROPIC_API_KEY` the planner uses a
deterministic offline backend, so the stack is fully demonstrable from a clean
clone. Set a key to plan with Claude.

### Local development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt
pip install -e .

cp .env.example .env               # optional

ruff check . && pytest -q
```

`make check` runs exactly what CI runs (`.\tasks.ps1 check` on PowerShell).

---

## Usage

### Workflow UI

Pick a role, type a request, and press **Plan it**. The UI shows the endpoints
that were offered to the model with their retrieval scores, the plan with risk
badges, the dry-run diff, an **Approve / Reject** control for anything gated, and
the complete audit trail.

### HTTP API

```bash
# Plan a request. High-risk plans stop here with a preview attached.
curl -sX POST localhost:8001/assistant/plan \
  -H 'content-type: application/json' \
  -d '{"request":"refund the last invoice for ana@acme.io","role":"billing_admin"}'

# Record a human decision.
curl -sX POST localhost:8001/assistant/runs/$RUN_ID/approve \
  -H 'content-type: application/json' \
  -d '{"step_id":"s3","approved":true,"decided_by":"lead@example.com"}'

# Resume. Re-plans and re-validates before executing.
curl -sX POST localhost:8001/assistant/runs/$RUN_ID/execute \
  -H 'content-type: application/json' \
  -d '{"request":"refund the last invoice for ana@acme.io","role":"billing_admin"}'

# Full audit trail for a run.
curl -s localhost:8001/assistant/runs/$RUN_ID
```

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/assistant/plan` | Plan a request and take it as far as is safe |
| `POST` | `/assistant/runs/{id}/approve` | Approve or reject one step |
| `POST` | `/assistant/runs/{id}/execute` | Run an approved plan |
| `GET` | `/assistant/runs` | Recent runs |
| `GET` | `/assistant/runs/{id}` | Full audit trail |

### As a library

The schema layer accepts any OpenAPI document. Because risk parsing fails
closed, a third-party specification loads with every operation marked maximally
dangerous:

```python
import httpx
from nl2api.schema.registry import ToolRegistry
from nl2api.schema.retriever import BM25Retriever

doc = httpx.get("https://petstore3.swagger.io/api/v3/openapi.json").json()
registry = ToolRegistry.from_openapi(doc)

print(len(registry), "operations")
print(BM25Retriever(registry).explain("add a new pet to the store"))
```

---

## Architecture

```
 request + caller role
        │
        ▼
 schema.retriever   BM25 over the OpenAPI document → ~6 candidate endpoints,
                    filtered to what this role may call
        ▼
 planner            LLM → strict JSON → Plan   (structured outputs; no regex)
        ▼
 guardrails         ① validator  schema match, narrow coercion, unknown-field reject
                    ② policy     role → risk → value ceiling → blast radius
                    ③ dry run    before/after in English, read-only by construction
        ▼
   high risk? ──yes──▶ persist as awaiting_approval, return the preview ──▶ human
        │                                                                    │
        no                                                            approves │
        ▼                                                                    ▼
 executor           resolve $steps references · re-validate resolved values ·
                    ambiguity halts · failure skips the rest
        ▼
 persistence        runs · run_steps · approvals · audit_events
        ▼
 answer + full audit trail
```

Order is itself a control: a plan that fails validation is never policy-checked,
and a plan policy denies is never previewed — so a forbidden step never reaches
even a read-only probe.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layer contracts.

---

## Design principles

**The schema is the contract.** Endpoints, parameters, response shapes *and* risk
level all come from one OpenAPI document. Risk travels as `x-risk` and
`x-required-roles` on each operation, so adding an endpoint forces you to declare
its blast radius in the same edit.

**Risk parsing fails closed.** An operation that declares no risk is treated as
`high_risk_write` requiring `billing_admin`. Forgetting to declare risk makes an
endpoint *harder* to call, never easier.

**Coercion is narrow.** `"24000"` becomes `24000` because the schema says
integer. `"$240"`, `"24,000"` and `"24000.5"` are errors. Unknown field names are
rejected outright, so a `skip_approval` field cannot be smuggled into a body.

**Retrieval is a capability boundary.** An operation that never reaches the
shortlist cannot appear in a plan, which narrows the blast radius of prompt
injection before validation runs. A plan referencing an unoffered operation is
rejected — that reach is the signal.

**Ambiguity is a question, not a guess.** Two customers share the name "Ana
Ruiz" in the seed data. The assistant asks which one; it never takes `data[0]`.

**Dry runs cannot mutate.** The preview builder receives an object whose entire
interface is one `read` method. There is no code path that could write.

**Approval is a row.** An approval that vanishes on restart is not an approval.
Resuming re-plans and re-validates rather than replaying a stored plan — an
approval is a decision about a plan *and* the state it was previewed against.

**Resolved values are re-validated.** A value copied out of an API response is
re-checked against the schema and re-run through the policy engine before it is
sent. An API response is no more trusted than the model.

The limits of all this are documented in [`docs/SAFETY.md`](docs/SAFETY.md).

---

## Configuration

Every setting is read from the environment or `.env`; see
[`.env.example`](.env.example) for the full list.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Unset means the offline planner is used |
| `NL2API_MODEL` | `claude-haiku-4-5` | Planning model |
| `NL2API_LLM_PROVIDER` | `anthropic` | `anthropic` · `cassette` · `rules` |
| `NL2API_DEFAULT_ROLE` | `support_agent` | `viewer` · `support_agent` · `billing_admin` |
| `NL2API_REFUND_MAX_CENTS` | `50000` | Refunds at or above this are refused outright |
| `NL2API_MAX_WRITE_STEPS` | `3` | Plans exceeding this many writes are refused |
| `NL2API_RETRIEVER_TOP_K` | `6` | Endpoints offered to the model per request |
| `NL2API_DATABASE_URL` | `sqlite:///./nl2api.db` | Change the URL for PostgreSQL |

---

## Testing

```bash
pytest -q                                # 355 tests, offline
pytest tests/test_golden.py -q            # 54 golden workflows
pytest --cov=nl2api --cov-report=term-missing
```

The suite requires no network and no API key — CI deliberately provides neither,
so the golden workflow results are reproducible from a clean checkout.

| File | Covers |
| --- | --- |
| `test_mock_api.py` | Endpoint contract and business rules |
| `test_schema.py` | OpenAPI parsing, fail-closed risk, retrieval |
| `test_planner.py` | Plan schema, backends, structural checks |
| `test_guardrails.py` | Coercion, validation, policy, dry runs, redaction |
| `test_executor.py` | Reference resolution, gates, failure handling |
| `test_service.py` | Assistant API and audit trail completeness |
| `test_golden.py` | 54 end-to-end workflows including the adversarial bucket |

---

## Project layout

```
src/nl2api/
├── mock_api/      business system being driven (17 endpoints, risk-tagged)
├── schema/        OpenAPI → typed tool catalogue + BM25 retrieval
├── planner/       request → strictly typed Plan (the LLM proposes)
├── guardrails/    validation, policy, dry runs, redaction (code decides)
├── executor/      typed workflow state, $steps resolution, the step loop
├── persistence/   audit tables: runs, steps, approvals, events
├── service/       the assistant's own HTTP API
└── ui/            Streamlit workflow front-end
```

---

## Stack

| Component | Choice |
| --- | --- |
| Language | Python 3.11+ |
| APIs | FastAPI |
| Contract | OpenAPI 3.1 / JSON Schema |
| LLM | Claude (Haiku 4.5 → Opus 5), structured outputs |
| Validation | Pydantic 2 + jsonschema |
| Storage | SQLAlchemy 2 — SQLite or PostgreSQL |
| UI | Streamlit |
| Tests | pytest, fully offline |
| Deployment | Docker Compose |

---

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Request lifecycle and layer contracts |
| [`docs/SAFETY.md`](docs/SAFETY.md) | Controls, threat model, and known limits |
| [`docs/DEMO.md`](docs/DEMO.md) | Guided walkthrough of the three core flows |

## License

MIT — see [LICENSE](LICENSE).
