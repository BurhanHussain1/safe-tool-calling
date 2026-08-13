# Architecture

## The one invariant

> **The LLM proposes. Deterministic code decides.**

Every design choice below follows from that sentence. If a change would let the
model's output reach the network without passing through the validator and the
policy engine, the change is wrong.

---

## Request lifecycle

```
 user request (English) + caller role
        │
        ▼
┌───────────────────┐
│ schema.retriever  │  BM25 over endpoint text → shortlist of ~6 ToolSpecs
└─────────┬─────────┘  (the model never sees all 16 endpoints at once)
          ▼
┌───────────────────┐
│ planner           │  LLM → strict JSON → Plan(steps=[PlanStep, ...])
└─────────┬─────────┘  structured outputs, so no regex and no retry-on-parse
          ▼
┌───────────────────┐
│ guardrails        │  ① validator  operation exists? params match schema?
│                   │               do $steps refs resolve?
│                   │  ② policy     role sufficient? risk → allow / notify /
│                   │               require-approval? blast radius sane?
│                   │  ③ dryrun     for each write: before/after in English
└─────────┬─────────┘  A rejection here ends the request. No HTTP is issued.
          ▼
    high risk? ──yes──▶ persist run as `awaiting_approval`, return preview
          │                          │
          no                     human approves
          │                          │
          ▼                          ▼
┌───────────────────┐
│ executor          │  topological order · resolve refs from WorkflowState ·
│                   │  call mock API · 0 or >1 matches → clarifying question ·
│                   │  failure → halt, mark rest `skipped`
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ persistence       │  runs · run_steps · approvals · audit_events
└─────────┬─────────┘  every run reconstructible from these four tables alone
          ▼
   final answer (English) + audit trail
```

---

## Layer contracts

| Layer | Input | Output | May it mutate data? |
| --- | --- | --- | --- |
| `schema` | OpenAPI dict | `ToolRegistry` of `ToolSpec` | no |
| `planner` | request, role, shortlist | `Plan` | no |
| `guardrails.validator` | `Plan`, registry | `ValidationReport` | no |
| `guardrails.policy` | `Plan`, role, settings | `PolicyDecision` per step | no |
| `guardrails.dryrun` | validated `Plan` | `DryRunPreview` per write | **no** — read-only calls only |
| `executor` | approved `Plan` | `WorkflowState` | yes — the only layer that may |
| `persistence` | anything above | rows | its own tables only |

`dryrun` being read-only is a tested property, not a convention: the test suite
snapshots the datastore before and after a dry run and asserts equality.

---

## Why risk metadata lives on the endpoint

`x-risk` and `x-required-roles` are OpenAPI extensions injected into each
operation by the mock API. Three consequences:

1. Adding an endpoint automatically declares its own risk — you cannot forget to
   register it in a separate table.
2. The assistant reads risk from the same document it reads parameters from, so
   there is one source of truth for the contract.
3. A reviewer can `curl /openapi.json` and see the entire security posture of the
   system in one response.

## Why the model never sees all endpoints

Retrieval is not only about token cost. A shortlist is a capability boundary: a
plan cannot reference `delete_customer` if `delete_customer` was never put in
front of the model for that request. It narrows the attack surface of prompt
injection before the validator even runs.

## Why chaining uses explicit references

A `PlanStep` says `{"customer_id": "$steps.s1.data[0].id"}`. Our resolver walks
that path against `WorkflowState`. The alternative — letting the model carry an
ID in its head across turns — puts an unvalidated value into a request. Explicit
references mean every value in an outgoing call was either written by the user,
written by the model *and validated*, or copied verbatim from a prior response.

## Why approvals are rows, not in-memory state

An approval that vanishes on restart is not an approval. Suspended runs persist
with their validated plan and dry-run preview attached; resuming re-validates
before executing, so a plan cannot be approved in one state of the world and
executed in another.
