# nl2api — Natural Language → API Assistant

> **Safe tool use over real API contracts.**

An LLM turns a sentence into a plan of API calls. Deterministic code — not the
model — validates that plan against an OpenAPI schema, previews what a write
would do in plain English, and blocks anything risky until a human approves it.

```
"Refund the last invoice for ana@acme.io"

  ├─ retrieved      search_customers · list_invoices · create_refund
  ├─ planned        3 steps, 1 high-risk write
  ├─ validated      ✓ schema  ✓ role: billing_admin  ✓ refs resolve
  ├─ dry run        "Refund $240.00 to Ana Ruiz (CUS-1001) against INV-1007.
  │                  Invoice would move paid → refunded. Not reversible."
  └─ BLOCKED        awaiting approval  ▸ [ Approve ]  [ Reject ]
```

---

## Status

Phase 0 of 6 complete. See **[PLAN.md](PLAN.md)** for the full build plan —
every phase has explicit deliverables, acceptance commands, and its own git
push block.

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Repo skeleton, config, CI | ✅ done |
| 1 | Mock SaaS admin API with risk metadata | ⬜ next |
| 2 | Schema parsing, retrieval, typed LLM plans | ⬜ |
| 3 | Validation, RBAC policy, dry runs, approvals | ⬜ |
| 4 | Multi-step workflow execution | ⬜ |
| 5 | Audit persistence, Streamlit UI, 50 golden tests | ⬜ |
| 6 | Docker Compose, safety docs, demo | ⬜ |

---

## Why this design

The hard part of tool calling is not getting a model to emit a function call.
It is making sure the call is one you would have allowed.

- **The schema is the contract.** Endpoints, parameters, response shapes *and*
  risk level all come from the OpenAPI document. The model gets a catalogue, not
  a vague list of capabilities.
- **The model proposes; code decides.** Every plan goes through
  `jsonschema` + Pydantic + a role/risk policy engine before a single HTTP
  request is made. A malformed or unauthorised plan never reaches the network.
- **Writes are previewed before they happen.** High-risk operations are
  dry-run against current state and described in English, then blocked pending
  approval that is persisted — so it survives a restart.
- **Ambiguity is a question, not a guess.** Two customers match that email?
  The assistant asks. It does not pick `[0]` and issue a refund.

---

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt -r requirements-dev.txt
pip install -e .

Copy-Item .env.example .env      # optional: add ANTHROPIC_API_KEY

ruff check .
pytest -q
```

The test suite runs **offline** — no API key, no network. The planner falls back
to recorded cassettes, which is what makes the golden workflow suite
reproducible in CI.

From Phase 6 onward the whole stack is one command:

```powershell
docker compose up --build
# mock API   http://localhost:8000/docs
# assistant  http://localhost:8001/docs
# UI         http://localhost:8501
```

---

## Tech stack

| Component | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11+ | Fast schema + API tooling |
| APIs | FastAPI | Generates the OpenAPI contract for free |
| Contract | OpenAPI / JSON Schema | The thing we validate against |
| LLM | Claude (Haiku 4.5 → Opus 5) | Structured outputs give a typed plan, no regex parsing |
| Validation | Pydantic 2 + jsonschema | Two layers: our types, then the endpoint's schema |
| Storage | SQLite → PostgreSQL | Runs, steps, approvals, audit events |
| UI | Streamlit | Shows plan → preview → approve → result |
| Tests | pytest + cassettes | Golden workflows, deterministic, offline |
| Deploy | Docker Compose | Three services, one command |

---

## Repository layout

See [PLAN.md § 2](PLAN.md) for the annotated tree and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the layers fit together.

---

## License

MIT — see [LICENSE](LICENSE).
