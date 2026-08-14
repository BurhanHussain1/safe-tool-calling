# safe-tool-calling

> **Safe tool use over real API contracts.**

An LLM turns a sentence into a plan of API calls. Deterministic code — not the
model — validates that plan against an OpenAPI schema, previews what a write
would do in plain English, and blocks anything risky until a human approves it.

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
| Multi-step chains | 12 | ids passed through typed state |
| Low-risk writes | 6 | executed and reported |
| High-risk writes | 8 | **blocked** pending human approval |
| Ambiguous | 6 | **asked**, zero writes |
| Injection / bulk destruction | 6 | **refused**, zero writes |
| Under-privileged caller | 4 | **refused**, zero writes |

For every case whose expected outcome is "nothing happened", the datastore is
snapshotted before and after and asserted byte-identical.

---

## Quickstart

```powershell
docker compose up --build
```

| | |
| --- | --- |
| UI | http://localhost:8501 |
| Assistant API | http://localhost:8001/docs |
| Mock business API | http://localhost:8000/docs |

**No API key needed.** Without `ANTHROPIC_API_KEY` the planner falls back to an
offline backend, so you can clone, run one command, and watch a refund get gated.
Set a key to plan with Claude.

<details>
<summary>Local development</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install -e .

ruff check . ; pytest -q
```

`.\tasks.ps1 check` runs exactly what CI runs. `make check` on bash/WSL.
</details>

---

## Architecture

```
 request + caller role
        │
        ▼
 schema.retriever   BM25 over the OpenAPI doc → ~6 candidate endpoints,
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
 executor           resolve $steps refs · re-validate resolved values ·
                    ambiguity halts · failure skips the rest
        ▼
 persistence        runs · run_steps · approvals · audit_events
        ▼
 answer + full audit trail
```

Order is a control: a plan that fails validation is never policy-checked, and a
plan policy denies is never previewed — so a forbidden step never reaches even a
read-only probe.

Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/SAFETY.md`](docs/SAFETY.md) · [`docs/DEMO.md`](docs/DEMO.md)

---

## Why this design

**The schema is the contract.** Endpoints, parameters, response shapes *and* risk
level all come from one OpenAPI document. Risk travels as `x-risk` /
`x-required-roles` on each operation, so adding an endpoint forces you to declare
its blast radius in the same edit.

**Risk parsing fails closed.** An operation that declares no risk is treated as
`high_risk_write` requiring `billing_admin`. Forgetting to declare risk makes an
endpoint *harder* to call. This is also why the parser works on third-party specs:

```python
import httpx
from nl2api.schema.registry import ToolRegistry
doc = httpx.get("https://petstore3.swagger.io/api/v3/openapi.json").json()
ToolRegistry.from_openapi(doc)   # every operation → maximally dangerous
```

**Coercion is narrow.** `"24000"` becomes `24000` because the schema says
integer. `"$240"`, `"24,000"` and `"24000.5"` are errors. A lenient parser is how
the wrong refund gets issued.

**Retrieval is a capability boundary.** An operation that never reaches the
shortlist cannot appear in a plan. A plan referencing an unoffered operation is
rejected — that reach is the signal we care about for injection.

**Ambiguity is a question.** Two customers are named Ana Ruiz in the seed data.
The assistant asks which one; it never takes `data[0]`.

**Approval is a row.** An approval that vanishes on restart is not an approval.
Resuming re-plans and re-validates rather than replaying — an approval is a
decision about a plan *and* the state it was previewed against.

**Resolved values are re-validated.** A value copied out of an API response is
re-checked against the schema and re-run through policy before it is sent. An API
response is no more trusted than the model.

The limits of all this are written down in [`docs/SAFETY.md`](docs/SAFETY.md).

---

## Stack

| Component | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11+ | Fast schema + API tooling |
| APIs | FastAPI | Generates the OpenAPI contract for free |
| Contract | OpenAPI / JSON Schema | The thing we validate against |
| LLM | Claude (Haiku 4.5 → Opus 5) | Structured outputs give a typed plan |
| Validation | Pydantic 2 + jsonschema | Our types, then the endpoint's schema |
| Storage | SQLite → PostgreSQL | Runs, steps, approvals, audit events |
| UI | Streamlit | Plan → preview → approve → result |
| Tests | pytest, offline | 54 golden workflows, deterministic |
| Deploy | Docker Compose | Three services, one command |

---

## Layout

```
src/nl2api/
├── mock_api/      the business system being driven (17 endpoints, risk-tagged)
├── schema/        OpenAPI → typed tool catalogue + BM25 retrieval
├── planner/       request → strictly typed Plan (LLM proposes)
├── guardrails/    validation, policy, dry runs, redaction (code decides)
├── executor/      typed workflow state, $steps resolution, the step loop
├── persistence/   four audit tables
├── service/       the assistant's own HTTP API
└── ui/            Streamlit workflow front-end
```

Build history and per-phase acceptance criteria: [`PLAN.md`](PLAN.md).

## License

MIT — see [LICENSE](LICENSE).
